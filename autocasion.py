#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Publicación automática del stock en Autocasión / AutoScout24 (API de Sumauto).

Compara el stock actual con lo que ya está publicado y manda SOLO lo que ha
cambiado: altas, modificaciones y bajas.

    python autocasion.py --diagnostico   # solo mira: empareja tus coches con su catalogo
    python autocasion.py --simular       # dice lo que haria, sin tocar el portal
    python autocasion.py                 # lo hace de verdad
    python autocasion.py --forzar        # salta la regla de seguridad

Credenciales (nunca en el codigo):
    SUMAUTO_USUARIO, SUMAUTO_CLAVE
    SUMAUTO_URL      opcional, si te dan una direccion distinta
    SUMAUTO_PRUEBAS  "1" para usar su endpoint de pruebas al emparejar
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import xml.etree.ElementTree as ET

import sumauto

FEED_MAESTRO = os.path.join("publico", "feed-maestro.xml")
FICHERO_ESTADO = "estado-autocasion.json"
FICHERO_EQUIVALENCIAS = "equivalencias-autocasion.csv"
FICHERO_SIN_EMPAREJAR = os.path.join("publico", "autocasion-sin-emparejar.csv")

MAXIMO_BAJAS_DE_GOLPE = 8
PORCENTAJE_BAJAS_SOSPECHOSO = 0.25

# Si cambia alguno de estos, hay que actualizar el anuncio. Son justo los que
# su API deja modificar sin volver a crearlo.
CAMPOS_VIGILADOS = ("precio", "km", "cuota_mes", "descripcion")


def log(mensaje):
    sello = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{sello}] {mensaje}", flush=True)


# --------------------------------------------------------------------------
# LEER EL STOCK
# --------------------------------------------------------------------------

def leer_feed_maestro(ruta=FEED_MAESTRO):
    if not os.path.exists(ruta):
        raise FileNotFoundError(
            f"No encuentro {ruta}. Ejecuta antes extraer_stock.py")

    raiz = ET.parse(ruta).getroot()
    stock = {}
    for n in raiz.findall("vehiculo"):
        def t(etiqueta, defecto=""):
            e = n.find(etiqueta)
            return (e.text or "").strip() if e is not None and e.text else defecto

        vid = t("id")
        if not vid:
            continue

        stock[vid] = {
            "id": vid,
            "matricula": t("matricula"),
            "bastidor": t("bastidor"),
            "marca": t("marca"),
            "modelo": t("modelo"),
            "version": t("version"),
            "anio": t("anio"),
            "mes": t("mes_matriculacion"),
            "km": t("km"),
            "precio": t("precio"),
            "iva_deducible": t("iva_deducible") == "true",
            "combustible": t("combustible"),
            "cambio": t("cambio"),
            "potencia_cv": t("potencia_cv"),
            "puertas": t("puertas"),
            "plazas": t("plazas"),
            "color": t("color"),
            "etiqueta_dgt": t("etiqueta_dgt"),
            "garantia_meses": t("garantia_meses"),
            "cuota_mes": t("cuota_mes"),
            "meses_financiacion": t("meses_financiacion"),
            "descripcion": t("descripcion"),
            "url": t("url"),
            "equipamiento": [i.text.strip() for i in n.findall("equipamiento/item")
                             if i.text and i.text.strip()],
            "fotos": [f.text.strip() for f in n.findall("fotos/foto")
                      if f.text and f.text.strip()],
        }
    return stock


# --------------------------------------------------------------------------
# ESTADO
# --------------------------------------------------------------------------

def cargar_estado(ruta=FICHERO_ESTADO):
    if not os.path.exists(ruta):
        return {"vehiculos": {}, "actualizado": None}
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
        datos.setdefault("vehiculos", {})
        return datos
    except (json.JSONDecodeError, OSError):
        log(f"  aviso: {ruta} ilegible. Se empieza de cero.")
        return {"vehiculos": {}, "actualizado": None}


def guardar_estado(estado, ruta=FICHERO_ESTADO):
    estado["actualizado"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


def huella(v):
    return "|".join(str(v.get(c, "")) for c in CAMPOS_VIGILADOS)


def cargar_equivalencias(ruta=None):
    """
    Hoja opcional para los coches que su catalogo no sabe emparejar solo:
        id,product_uuid
    """
    ruta = ruta or FICHERO_EQUIVALENCIAS
    if not os.path.exists(ruta):
        return {}
    equivalencias = {}
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            fila = {(k or "").strip().lower(): (v or "").strip()
                    for k, v in fila.items()}
            if fila.get("id") and fila.get("product_uuid"):
                equivalencias[fila["id"]] = fila["product_uuid"]
    if equivalencias:
        log(f"  equivalencias manuales cargadas: {len(equivalencias)}")
    return equivalencias


# --------------------------------------------------------------------------
# CONSTRUIR EL ANUNCIO QUE ENTIENDE SU API
# --------------------------------------------------------------------------

def _entero(valor, defecto=0):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return defecto


def fecha_matriculacion(v):
    """Su formato es AAAA/MM/DD. Nosotros solo sabemos ano y mes."""
    anio, mes = _entero(v.get("anio")), _entero(v.get("mes"))
    if not anio:
        return ""
    mes = mes if 1 <= mes <= 12 else 1
    return f"{anio:04d}/{mes:02d}/01"


def texto_financiacion(v):
    cuota = _entero(v.get("cuota_mes"))
    if not cuota:
        return ""
    meses = _entero(v.get("meses_financiacion"), 72)
    return (f"Cuota orientativa desde {cuota} EUR/mes a {meses} meses, "
            f"sin entrada y sujeta a aprobacion de la financiera.")


def construir_anuncio(v, api, product_uuid=None, solo_modificables=False):
    """
    Monta el objeto que espera su API.
    Con solo_modificables=True devuelve lo que admite la edicion (AdBase),
    que es un subconjunto: ni matricula, ni ano, ni ficha de catalogo.
    """
    equipamiento = ", ".join(v.get("equipamiento", []))[:5000]

    anuncio = {
        "price": _entero(v.get("precio")),
        "kilometers": _entero(v.get("km")),
        "description": (v.get("descripcion") or "")[:10000],
        "equipment": equipamiento,
        "is_automatic": sumauto.normalizar(v.get("cambio")).startswith("automat"),
        "warranty": True,
        "warranty_length": _entero(v.get("garantia_meses"), 12),
        "tax_free": bool(v.get("iva_deducible")),
        "chassis_number": (v.get("bastidor") or "")[:17],
        "color_txt": v.get("color") or "",
        "metallic_color": False,
    }

    color_id = api.id_color(v.get("color")) if api else None
    if color_id:
        anuncio["color"] = color_id

    cuota = _entero(v.get("cuota_mes"))
    if cuota:
        anuncio["financed_price"] = cuota
        anuncio["financed_comment"] = texto_financiacion(v)

    if solo_modificables:
        return anuncio

    anuncio.update({
        "reference": v["id"],
        "type": "ocasion",
        "plate": v.get("matricula") or "",
        "year": _entero(v.get("anio")),
        "plate_date": fecha_matriculacion(v),
    })
    if product_uuid:
        anuncio["product_uuid"] = product_uuid
    return anuncio


# --------------------------------------------------------------------------
# QUE HAY QUE HACER
# --------------------------------------------------------------------------

def calcular_cambios(stock, estado):
    publicados = estado.get("vehiculos", {})

    altas = [v for vid, v in stock.items() if vid not in publicados]

    cambios = []
    for vid, v in stock.items():
        anterior = publicados.get(vid)
        if anterior and anterior.get("huella") != huella(v):
            cambios.append({"vehiculo": v,
                            "antes": anterior.get("huella", ""),
                            "ahora": huella(v),
                            "referencia": anterior.get("referencia", vid),
                            "uuid": anterior.get("uuid")})

    bajas = [{"id": vid, "referencia": datos.get("referencia", vid),
              "uuid": datos.get("uuid"), "version": datos.get("version", "")}
             for vid, datos in publicados.items() if vid not in stock]

    return {"altas": altas, "cambios": cambios, "bajas": bajas}


def pasa_control(cambios, total_publicados, forzar=False):
    n_bajas = len(cambios["bajas"])
    if n_bajas == 0:
        return True, "sin bajas que revisar"

    proporcion = n_bajas / total_publicados if total_publicados else 1
    sospechoso = n_bajas > MAXIMO_BAJAS_DE_GOLPE
    if total_publicados >= 20 and proporcion > PORCENTAJE_BAJAS_SOSPECHOSO:
        sospechoso = True

    if not sospechoso:
        return True, f"{n_bajas} bajas, dentro de lo normal"

    motivo = (f"habria que retirar {n_bajas} anuncios de {total_publicados} "
              f"({round(proporcion*100)}%), y eso no es un dia normal de ventas")
    if forzar:
        return True, f"FORZADO pese a que {motivo}"
    return False, motivo


# --------------------------------------------------------------------------
# EMPAREJAR TODO EL STOCK CON SU CATALOGO
# --------------------------------------------------------------------------

def emparejar_stock(stock, api, equivalencias, pruebas=False, limite=None):
    emparejados, fallidos = {}, []
    vehiculos = list(stock.values())
    if limite:
        vehiculos = vehiculos[:limite]

    for i, v in enumerate(vehiculos, 1):
        if v["id"] in equivalencias:
            emparejados[v["id"]] = equivalencias[v["id"]]
            continue

        uuid, explicacion = api.buscar_producto(v, pruebas=pruebas)
        if uuid:
            emparejados[v["id"]] = uuid
        else:
            fallidos.append({"id": v["id"], "matricula": v.get("matricula", ""),
                             "vehiculo": v.get("version", ""),
                             "marca": v.get("marca", ""), "modelo": v.get("modelo", ""),
                             "motivo": explicacion})
        if i % 10 == 0:
            log(f"  emparejados {i} de {len(vehiculos)}")

    if fallidos:
        os.makedirs(os.path.dirname(FICHERO_SIN_EMPAREJAR), exist_ok=True)
        with open(FICHERO_SIN_EMPAREJAR, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "matricula", "marca", "modelo",
                                              "vehiculo", "motivo"])
            w.writeheader()
            w.writerows(fallidos)

    return emparejados, fallidos


# --------------------------------------------------------------------------
# PRINCIPAL
# --------------------------------------------------------------------------

def credenciales():
    usuario = os.environ.get("SUMAUTO_USUARIO")
    clave = os.environ.get("SUMAUTO_CLAVE")
    if not usuario or not clave:
        raise RuntimeError(
            "Faltan las credenciales. Define SUMAUTO_USUARIO y SUMAUTO_CLAVE "
            "(en GitHub van en Settings -> Secrets and variables -> Actions).")
    return usuario, clave


def main():
    p = argparse.ArgumentParser(description="Publica el stock en Autocasion")
    p.add_argument("--diagnostico", action="store_true",
                   help="solo comprueba el emparejamiento con su catalogo")
    p.add_argument("--simular", action="store_true",
                   help="dice lo que haria, sin tocar el portal")
    p.add_argument("--forzar", action="store_true")
    p.add_argument("--feed", default=FEED_MAESTRO)
    p.add_argument("--estado", default=FICHERO_ESTADO)
    p.add_argument("--limite", type=int, default=None,
                   help="trabaja solo con los primeros N coches (para probar)")
    args = p.parse_args()

    log("=" * 64)
    stock = leer_feed_maestro(args.feed)
    estado = cargar_estado(args.estado)
    publicados = estado["vehiculos"]
    equivalencias = cargar_equivalencias()
    log(f"Autocasion | en tu web: {len(stock)} | publicados: {len(publicados)}")

    # ---------------------------------------------------------- diagnostico
    if args.diagnostico:
        usuario, clave = credenciales()
        api = sumauto.Sumauto(usuario, clave)
        api.entrar()
        api.cargar_catalogos()

        log("Emparejando tu stock con su catalogo...")
        emparejados, fallidos = emparejar_stock(
            stock, api, equivalencias,
            pruebas=os.environ.get("SUMAUTO_PRUEBAS") == "1",
            limite=args.limite)

        total = len(emparejados) + len(fallidos)
        log(f"  RESULTADO: {len(emparejados)} de {total} emparejados")
        if fallidos:
            log(f"  {len(fallidos)} sin emparejar, listados en {FICHERO_SIN_EMPAREJAR}:")
            for f in fallidos[:15]:
                log(f"    {f['matricula'] or f['id']}  {f['marca']} {f['modelo']}"
                    f"  -> {f['motivo']}")
            if len(fallidos) > 15:
                log(f"    ... y {len(fallidos)-15} mas en el fichero")
        log("Diagnostico terminado. No se ha publicado nada.")
        return 0

    # -------------------------------------------------------------- cambios
    cambios = calcular_cambios(stock, estado)
    log(f"  altas: {len(cambios['altas'])} | modificaciones: {len(cambios['cambios'])} "
        f"| bajas: {len(cambios['bajas'])}")

    for v in cambios["altas"]:
        log(f"    ALTA    {v['matricula'] or v['id']}  {v['version'][:55]}")
    for c in cambios["cambios"]:
        v = c["vehiculo"]
        log(f"    CAMBIO  {v['matricula'] or v['id']}  {v['version'][:40]}")
    for b in cambios["bajas"]:
        log(f"    BAJA    {b['referencia']}  {b['version'][:55]}")

    if not any(cambios.values()):
        log("  No hay nada que mandar. Todo al dia.")
        return 0

    ok, motivo = pasa_control(cambios, len(publicados), args.forzar)
    if not ok:
        log(f"  ABORTADO: {motivo}")
        log("  No se ha tocado nada en el portal.")
        log("  Si de verdad se han vendido todos esos, repite con --forzar")
        return 1
    log(f"  control de seguridad: {motivo}")

    if args.simular:
        log("  MODO SIMULACION: no se ha llamado al portal.")
        return 0

    # ------------------------------------------------------------ ejecutar
    usuario, clave = credenciales()
    api = sumauto.Sumauto(usuario, clave)
    api.entrar()
    api.cargar_catalogos()

    errores = 0
    pruebas = os.environ.get("SUMAUTO_PRUEBAS") == "1"

    for v in cambios["altas"]:
        etiqueta = v["matricula"] or v["id"]
        try:
            product_uuid = equivalencias.get(v["id"])
            if not product_uuid:
                product_uuid, explicacion = api.buscar_producto(v, pruebas=pruebas)
                if not product_uuid:
                    log(f"    SIN EMPAREJAR {etiqueta}: {explicacion}")
                    errores += 1
                    continue
                log(f"    {etiqueta} -> {explicacion}")

            anuncio = construir_anuncio(v, api, product_uuid)
            api.publicar(anuncio)

            resultado_fotos = api.subir_fotos(v["id"], v.get("fotos", []))
            fallos_foto = [r for r in (resultado_fotos or [])
                           if isinstance(r, dict) and r.get("result") == "KO"]

            publicados[v["id"]] = {
                "referencia": v["id"],
                "huella": huella(v),
                "version": v["version"],
                "publicado": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            aviso = f" ({len(fallos_foto)} fotos fallidas)" if fallos_foto else ""
            log(f"    publicado {etiqueta}{aviso}")
        except Exception as e:
            errores += 1
            log(f"    ERROR al publicar {etiqueta}: {e}")

    for c in cambios["cambios"]:
        v = c["vehiculo"]
        etiqueta = v["matricula"] or v["id"]
        try:
            uuid = c.get("uuid")
            if not uuid:
                ficha = api.anuncio_por_referencia(c["referencia"])
                uuid = (ficha or {}).get("ad", {}).get("uuid") if ficha else None
            if not uuid:
                log(f"    no encuentro el anuncio de {etiqueta} en su sistema")
                errores += 1
                continue

            api.modificar(uuid, construir_anuncio(v, api, solo_modificables=True))
            publicados[v["id"]].update({"huella": huella(v), "uuid": uuid,
                                        "version": v["version"]})
            log(f"    actualizado {etiqueta}")
        except Exception as e:
            errores += 1
            log(f"    ERROR al actualizar {etiqueta}: {e}")

    for b in cambios["bajas"]:
        try:
            uuid = b.get("uuid")
            if not uuid:
                ficha = api.anuncio_por_referencia(b["referencia"])
                uuid = (ficha or {}).get("ad", {}).get("uuid") if ficha else None
            if not uuid:
                log(f"    el anuncio {b['referencia']} ya no esta en su sistema")
                publicados.pop(b["id"], None)
                continue
            api.retirar(uuid)
            publicados.pop(b["id"], None)
            log(f"    retirado {b['referencia']}")
        except Exception as e:
            errores += 1
            log(f"    ERROR al retirar {b['referencia']}: {e}")

    guardar_estado(estado, args.estado)
    log(f"  estado guardado: {len(publicados)} anuncios activos")

    try:
        pendientes = api.errores()
        if pendientes:
            log(f"  OJO: su sistema reporta errores en anuncios. "
                f"Revisa /errors o el panel de Autocasion.")
    except RuntimeError:
        pass

    if errores:
        log(f"  TERMINADO CON {errores} ERRORES. Revisa el detalle de arriba.")
        return 1
    log("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
