#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cliente de la API de Listings de Sumauto (Autocasion / AutoScout24).

Se encarga de hablar con ellos: identificarse, consultar sus catalogos,
emparejar cada coche con su ficha de catalogo, y publicar, modificar o
retirar anuncios.

Solo libreria estandar de Python 3.
"""

import datetime as dt
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SUMAUTO_URL", "https://api-listing.sumauto.com")
TIMEOUT = 30
REINTENTOS = 3
PAUSA = 0.2

FICHERO_CATALOGOS = os.path.join(".cache", "catalogos-sumauto.json")
DIAS_CATALOGO_VALIDO = 7


def log(mensaje):
    sello = dt.datetime.now().strftime("%H:%M:%S")
    print(f"  [{sello}] {mensaje}", flush=True)


# --------------------------------------------------------------------------
# COMPARAR TEXTOS (sus catalogos y tu web no escriben igual)
# --------------------------------------------------------------------------

def normalizar(texto):
    """'Volkswagen  Caddy-Maxi' -> 'volkswagen caddy maxi'"""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Marcas que tu web escribe de una forma y el catalogo de otra
ALIAS_MARCAS = {
    "vw": "volkswagen",
    "volkwagen": "volkswagen",          # erratas reales de tu stock
    "mercedes": "mercedes benz",
    "citroen": "citroen",
    "land rover": "land rover",
}


def parecido(a, b):
    """0 a 1. Cuantas palabras de la mas corta estan en la otra."""
    pa, pb = set(normalizar(a).split()), set(normalizar(b).split())
    if not pa or not pb:
        return 0.0
    return len(pa & pb) / min(len(pa), len(pb))


# --------------------------------------------------------------------------
# CLIENTE
# --------------------------------------------------------------------------

class Sumauto:

    def __init__(self, usuario, clave, base=BASE):
        self.usuario = usuario
        self.clave = clave
        self.base = base.rstrip("/")
        self.token = None
        self.refresco = None
        self.catalogos = {}

    # --------------------------------------------------------------- red

    def _peticion(self, metodo, ruta, cuerpo=None, parametros=None,
                  cabeceras_extra=None, reintentar_token=True):
        url = self.base + ruta
        if parametros:
            limpios = {k: v for k, v in parametros.items() if v is not None}
            url += "?" + urllib.parse.urlencode(limpios)

        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        cabeceras = {"Accept": "application/json",
                     "User-Agent": "MidCarFeed/1.0 (ventas@midcar.net)"}
        if datos:
            cabeceras["Content-Type"] = "application/json"
        if self.token:
            cabeceras["token"] = self.token
        if cabeceras_extra:
            cabeceras.update(cabeceras_extra)

        ultimo = None
        for intento in range(1, REINTENTOS + 1):
            peticion = urllib.request.Request(url, data=datos, headers=cabeceras,
                                              method=metodo)
            try:
                with urllib.request.urlopen(peticion, timeout=TIMEOUT) as r:
                    bruto = r.read().decode("utf-8") or "null"
                    return r.status, json.loads(bruto)
            except urllib.error.HTTPError as e:
                detalle = e.read().decode("utf-8", "replace")[:500]

                # Token caducado: renovamos una vez y repetimos
                if e.code in (401, 403) and reintentar_token and self.refresco:
                    log("token caducado, renovando")
                    self.renovar()
                    cabeceras["token"] = self.token
                    reintentar_token = False
                    continue

                # Errores del cliente: no insistimos, es culpa nuestra
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    raise RuntimeError(f"{metodo} {ruta} -> {e.code}: {detalle}")

                ultimo = RuntimeError(f"{metodo} {ruta} -> {e.code}: {detalle}")
            except (urllib.error.URLError, TimeoutError) as e:
                ultimo = RuntimeError(f"{metodo} {ruta} -> sin respuesta: {e}")

            espera = intento * 2
            log(f"fallo en {ruta}, reintento en {espera}s")
            time.sleep(espera)

        raise ultimo

    # ------------------------------------------------------- identificarse

    def entrar(self):
        _, datos = self._peticion("POST", "/token",
                                  cuerpo={"username": self.usuario,
                                          "password": self.clave},
                                  reintentar_token=False)
        self.token, self.refresco = self._sacar_tokens(datos)
        if not self.token:
            raise RuntimeError(f"No encuentro el token en su respuesta: "
                               f"{json.dumps(datos)[:300]}")
        log("identificado correctamente")
        return self.token

    def renovar(self):
        if not self.refresco:
            return self.entrar()
        try:
            _, datos = self._peticion("POST", "/refresh",
                                      cabeceras_extra={"refresh": self.refresco},
                                      reintentar_token=False)
            nuevo, refresco = self._sacar_tokens(datos)
            if nuevo:
                self.token = nuevo
                self.refresco = refresco or self.refresco
                return self.token
        except RuntimeError as e:
            log(f"no se pudo renovar ({e}); entrando de nuevo")
        return self.entrar()

    @staticmethod
    def _sacar_tokens(datos):
        """Su documentacion no fija el formato, asi que admitimos varios."""
        if isinstance(datos, str):
            return datos, None
        if not isinstance(datos, dict):
            return None, None
        plano = {k.lower().replace("_", ""): v for k, v in datos.items()}
        token = (plano.get("token") or plano.get("accesstoken")
                 or plano.get("jwt") or plano.get("acceso"))
        refresco = plano.get("refresh") or plano.get("refreshtoken")
        if isinstance(token, dict):
            token = token.get("token") or token.get("access_token")
        return token, refresco

    # ----------------------------------------------------------- catalogos

    def cargar_catalogos(self, forzar=False):
        """Marcas, carrocerias, combustibles, colores y cambios. Se cachean."""
        if os.path.exists(FICHERO_CATALOGOS) and not forzar:
            try:
                with open(FICHERO_CATALOGOS, encoding="utf-8") as f:
                    guardado = json.load(f)
                edad = dt.datetime.now() - dt.datetime.fromisoformat(guardado["fecha"])
                if edad.days < DIAS_CATALOGO_VALIDO:
                    self.catalogos = guardado["catalogos"]
                    log(f"catalogos reaprovechados ({edad.days} dias)")
                    return self.catalogos
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                pass

        log("descargando catalogos de Sumauto")
        self.catalogos = {
            "brands": self._peticion("GET", "/brands")[1] or [],
            "bodyworks": self._peticion("GET", "/bodyworks")[1] or [],
            "fuels": self._peticion("GET", "/fuels")[1] or [],
            "colors": self._peticion("GET", "/colors")[1] or [],
            "transmissions": self._peticion("GET", "/transmisions")[1] or [],
            "families": {},
        }
        os.makedirs(os.path.dirname(FICHERO_CATALOGOS), exist_ok=True)
        with open(FICHERO_CATALOGOS, "w", encoding="utf-8") as f:
            json.dump({"fecha": dt.datetime.now().isoformat(),
                       "catalogos": self.catalogos}, f, ensure_ascii=False)
        log(f"catalogos: {len(self.catalogos['brands'])} marcas, "
            f"{len(self.catalogos['colors'])} colores")
        return self.catalogos

    def familias(self, brand_id):
        clave = str(brand_id)
        guardadas = self.catalogos.setdefault("families", {})
        if clave not in guardadas:
            guardadas[clave] = self._peticion(
                "GET", f"/brand/{brand_id}/families")[1] or []
            time.sleep(PAUSA)
        return guardadas[clave]

    # ------------------------------------------------- buscar en catalogos

    def _buscar(self, lista, texto, minimo=0.6):
        """Devuelve el elemento del catalogo que mejor encaja con el texto."""
        objetivo = normalizar(texto)
        objetivo = ALIAS_MARCAS.get(objetivo, objetivo)
        if not objetivo:
            return None

        for elemento in lista:
            if normalizar(elemento.get("name")) == objetivo:
                return elemento

        mejor, mejor_nota = None, 0.0
        for elemento in lista:
            nota = parecido(objetivo, elemento.get("name"))
            if nota > mejor_nota:
                mejor, mejor_nota = elemento, nota
        return mejor if mejor_nota >= minimo else None

    def id_marca(self, marca):
        e = self._buscar(self.catalogos.get("brands", []), marca)
        return e["id"] if e else None

    def id_familia(self, brand_id, modelo):
        e = self._buscar(self.familias(brand_id), modelo)
        return e["id"] if e else None

    def id_color(self, color):
        e = self._buscar(self.catalogos.get("colors", []), color)
        return e["id"] if e else None

    def id_combustible(self, combustible):
        equivalencias = {
            "diesel": "diesel", "gasolina": "gasolina",
            "hibrido": "hibrido", "electrico": "electrico",
            "gas": "glp",
        }
        t = equivalencias.get(normalizar(combustible), normalizar(combustible))
        e = self._buscar(self.catalogos.get("fuels", []), t, minimo=0.5)
        return e["id"] if e else None

    def id_cambio(self, cambio):
        e = self._buscar(self.catalogos.get("transmissions", []), cambio)
        return e["id"] if e else None

    # ------------------------------------------- emparejar con su catalogo

    def buscar_producto(self, v, pruebas=False):
        """
        Encuentra la ficha de catalogo (product_uuid) que corresponde al coche.
        Devuelve (uuid, explicacion). Si no la encuentra, (None, motivo).
        """
        brand_id = self.id_marca(v.get("marca"))
        if not brand_id:
            return None, f"marca desconocida para ellos: '{v.get('marca')}'"

        family_id = self.id_familia(brand_id, v.get("modelo"))
        if not family_id:
            return None, f"modelo desconocido para ellos: '{v.get('modelo')}'"

        ruta = "/test/matchproduct" if pruebas else "/matchproduct"

        # De mas filtros a menos: si no hay suerte, aflojamos
        intentos = [
            {"fuel": self.id_combustible(v.get("combustible")),
             "transmission": self.id_cambio(v.get("cambio")),
             "horse_power": _entero(v.get("potencia_cv")),
             "doors": _entero(v.get("puertas")),
             "seats": _entero(v.get("plazas")),
             "plate_date_year": _entero(v.get("anio")),
             "plate_date_month": _entero(v.get("mes"))},
            {"fuel": self.id_combustible(v.get("combustible")),
             "horse_power": _entero(v.get("potencia_cv"))},
            {},
        ]

        for n, filtros in enumerate(intentos, 1):
            parametros = {"brand": brand_id, "family": family_id,
                          "version": v.get("version", "")}
            parametros.update(filtros)
            try:
                _, productos = self._peticion("GET", ruta, parametros=parametros)
            except RuntimeError as e:
                return None, f"error al emparejar: {e}"

            productos = [p for p in (productos or []) if isinstance(p, dict) and p.get("uuid")]
            if not productos:
                continue

            elegido = self._mejor_producto(productos, v)
            holgura = "exacta" if n == 1 else f"con menos filtros (intento {n})"
            return elegido["uuid"], f"{elegido.get('name','?')} [{holgura}]"

        return None, "su catalogo no devuelve ninguna version que encaje"

    @staticmethod
    def _mejor_producto(productos, v):
        """Entre varias versiones suyas, la que mas se parece a la tuya."""
        objetivo = v.get("version", "")
        cv = _entero(v.get("potencia_cv"))

        def nota(p):
            n = parecido(objetivo, p.get("name"))
            if cv and p.get("horse_power") == cv:
                n += 0.3
            return n

        return max(productos, key=nota)

    # --------------------------------------------------------------- anuncios

    def anuncio_por_referencia(self, referencia):
        """Su ficha del anuncio que publicamos con esa referencia nuestra."""
        try:
            _, datos = self._peticion("GET", f"/ad/ref/{urllib.parse.quote(str(referencia))}")
            return datos
        except RuntimeError:
            return None

    def publicar(self, anuncio):
        codigo, datos = self._peticion("POST", "/ad", cuerpo=anuncio)
        return codigo, datos

    def modificar(self, uuid, anuncio):
        return self._peticion("PUT", f"/ad/{uuid}", cuerpo=anuncio)

    def retirar(self, ad_id):
        return self._peticion("DELETE", f"/ad/{ad_id}")

    def stock_publicado(self, solo_publicados=True):
        """Todos tus anuncios en su sistema, pagina a pagina."""
        todos, pagina = [], 1
        while True:
            _, lote = self._peticion("GET", "/stock",
                                     parametros={"page": pagina,
                                                 "only_published": solo_publicados})
            if not lote:
                break
            todos.extend(lote)
            if len(lote) < 2:      # su paginacion no declara tamano fijo
                break
            pagina += 1
            if pagina > 100:
                break
            time.sleep(PAUSA)
        return todos

    def errores(self, pagina=1):
        _, datos = self._peticion("GET", "/errors", parametros={"page": pagina})
        return datos

    # ----------------------------------------------------------------- fotos

    def subir_fotos(self, referencia, urls):
        """Les pasamos las direcciones y ellos se descargan las imagenes."""
        if not urls:
            return []
        cuerpo = {"reference": str(referencia),
                  "ad_medias": [{"path": u} for u in urls]}
        _, datos = self._peticion("POST", "/bulk/media", cuerpo=cuerpo)
        return datos or []

    def borrar_fotos(self, referencia, uuids):
        if not uuids:
            return None
        cuerpo = {"reference": str(referencia), "ad_medias": list(uuids)}
        return self._peticion("DELETE", "/bulk/media", cuerpo=cuerpo)[1]

    def fotos_de(self, uuid_anuncio):
        _, datos = self._peticion("GET", f"/media/ad/{uuid_anuncio}")
        return datos or []


def _entero(valor):
    try:
        n = int(float(valor))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None
