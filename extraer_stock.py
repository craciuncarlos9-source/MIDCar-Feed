#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generador del feed de stock de MID Car (midcar.net).

Lee el stock desde el API publico de la propia web y genera:

  publico/feed-maestro.xml   -> feed maestro con todos los datos
  publico/stock.csv          -> el mismo stock en tabla, para revisar a ojo
  publico/resumen.json       -> cifras del ultimo pase (para el aviso)
  registro.log               -> historico de ejecuciones

Solo usa la libreria estandar de Python 3. No hay que instalar nada.

Uso:
    python extraer_stock.py                 # pase normal
    python extraer_stock.py --forzar        # ignora la regla de seguridad
    python extraer_stock.py --sin-cache     # vuelve a bajar todas las fichas
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.dom import minidom

import adaptadores

# --------------------------------------------------------------------------
# CONFIGURACION
# --------------------------------------------------------------------------

API = "https://api.midcar.net"
WEB = "https://www.midcar.net"

# Estados del vehiculo tal y como los devuelve el API.
#   0 = disponible
#   1 = reservado
#   2 = vendido
# IMPORTANTE: confirma este significado en tu panel antes de publicar el feed
# de verdad. Es la unica suposicion del programa.
ESTADOS_PUBLICABLES = {0}
ESTADO_RESERVADO = 1

# Regla de seguridad: si el stock cae por debajo de estos limites respecto al
# ultimo feed valido, NO se publica nada y se conserva el feed anterior.
MINIMO_ABSOLUTO = 10          # menos coches que esto = algo va mal
CAIDA_MAXIMA = 0.30           # perder mas del 30% de golpe = algo va mal

PAUSA_ENTRE_PETICIONES = 0.15  # segundos, para no cargar el servidor
REINTENTOS = 3
TIMEOUT = 30

CARPETA_SALIDA = "publico"
CARPETA_CACHE = ".cache"
FICHERO_EXTRA = "datos-extra.csv"      # opcional: bastidor y garantia, en tabla
FICHERO_BASTIDORES = "bastidores.txt"  # opcional: una linea por coche, "matricula bastidor"

# Codigo de la tienda en Merchant Center, tal y como lo tengas dado de alta
# en tu perfil de empresa de Google. Distingue mayusculas de minusculas.
STORE_CODE = "torrejon-de-ardoz"

AGENTE = "MidCarFeed/1.0 (generador de feed propio; ventas@midcar.net)"


# --------------------------------------------------------------------------
# UTILIDADES
# --------------------------------------------------------------------------

def log(mensaje):
    sello = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linea = f"[{sello}] {mensaje}"
    print(linea, flush=True)
    with open("registro.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def pedir(url, cuerpo=None):
    """GET o POST al API, con reintentos."""
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    cabeceras = {"User-Agent": AGENTE, "Accept": "application/json"}
    if datos:
        cabeceras["Content-Type"] = "application/json"

    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            peticion = urllib.request.Request(url, data=datos, headers=cabeceras)
            with urllib.request.urlopen(peticion, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            ultimo_error = e
            espera = intento * 2
            log(f"  aviso: fallo al pedir {url} ({e}). Reintento en {espera}s")
            time.sleep(espera)
    raise RuntimeError(f"No se pudo acceder a {url}: {ultimo_error}")


def limpiar(texto):
    if texto is None:
        return ""
    return re.sub(r"[ \t]+", " ", str(texto)).strip()


def entero(valor, defecto=0):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return defecto


# --------------------------------------------------------------------------
# LECTURA DEL STOCK
# --------------------------------------------------------------------------

def bajar_resumenes():
    """Trae la lista completa de vehiculos, pagina a pagina."""
    todos = []
    pagina = 1
    while True:
        cuerpo = {
            "page": pagina,
            "pageSize": 200,
            "orderByField": "CreatedDate",
            "orderByAscending": False,
        }
        datos = pedir(f"{API}/vehicles/search-summaries", cuerpo)
        lote = datos.get("items", [])
        todos.extend(lote)
        total = datos.get("total", len(todos))
        log(f"  pagina {pagina}: {len(lote)} vehiculos (acumulado {len(todos)} de {total})")
        if len(todos) >= total or not lote:
            break
        pagina += 1
        time.sleep(PAUSA_ENTRE_PETICIONES)
    return todos


def cargar_cache():
    ruta = os.path.join(CARPETA_CACHE, "detalles.json")
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def guardar_cache(cache):
    os.makedirs(CARPETA_CACHE, exist_ok=True)
    with open(os.path.join(CARPETA_CACHE, "detalles.json"), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def huella(resumen):
    """Si esto no cambia, la ficha no ha cambiado: reaprovechamos la cache."""
    return "|".join(str(resumen.get(c, "")) for c in
                    ("price", "mileage", "vehicleStatus", "financingPrice", "uri"))


def bajar_fichas(resumenes, usar_cache=True):
    """Trae la ficha completa de cada vehiculo publicable."""
    cache = cargar_cache() if usar_cache else {}
    fichas = []
    reaprovechados = 0

    for i, r in enumerate(resumenes, 1):
        vid = str(r.get("id"))
        clave = huella(r)
        guardado = cache.get(vid)

        if guardado and guardado.get("_huella") == clave:
            fichas.append(guardado["ficha"])
            reaprovechados += 1
            continue

        try:
            ficha = pedir(f"{API}/vehicles/{r['uri']}")
        except RuntimeError as e:
            log(f"  aviso: no se pudo leer la ficha de {vid} ({e}). Se omite.")
            continue

        fichas.append(ficha)
        cache[vid] = {"_huella": clave, "ficha": ficha}
        time.sleep(PAUSA_ENTRE_PETICIONES)

        if i % 20 == 0:
            log(f"  fichas leidas: {i} de {len(resumenes)}")

    guardar_cache(cache)
    log(f"  fichas nuevas: {len(fichas) - reaprovechados} | reaprovechadas de cache: {reaprovechados}")
    return fichas


# --------------------------------------------------------------------------
# DATOS EXTRA (bastidor, garantia) DESDE HOJA DE CALCULO
# --------------------------------------------------------------------------

def normalizar_matricula(texto):
    return re.sub(r"[^A-Z0-9]", "", limpiar(texto).upper())


def cargar_bastidores_txt():
    """
    Lista sencilla: una linea por coche con la matricula y el bastidor,
    separados por un espacio, una coma, un punto y coma o un tabulador.

        3051LWX VF3VFAHKKLZ012345
        5502LZP, VXKUHSH7639201923

    Las lineas vacias y las que empiezan por # se ignoran.
    Devuelve {matricula_normalizada: bastidor}.
    """
    if not os.path.exists(FICHERO_BASTIDORES):
        return {}

    encontrados, malas = {}, []
    with open(FICHERO_BASTIDORES, encoding="utf-8-sig") as f:
        for n, linea in enumerate(f, 1):
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            partes = [p for p in re.split(r"[\s,;\t]+", linea) if p]
            if len(partes) < 2:
                malas.append((n, linea))
                continue

            # El bastidor es lo ultimo de la linea; todo lo de delante es la
            # matricula, aunque la hayas escrito con un espacio en medio.
            bastidor = re.sub(r"[^A-Za-z0-9]", "", partes[-1]).upper()
            matricula = normalizar_matricula(" ".join(partes[:-1]))

            if len(bastidor) < 10 or not (4 <= len(matricula) <= 10):
                malas.append((n, linea))
                continue
            if len(bastidor) != 17:
                log(f"  aviso: linea {n} de {FICHERO_BASTIDORES}: el bastidor "
                    f"'{bastidor}' tiene {len(bastidor)} caracteres y deberian ser 17")
            encontrados[matricula] = bastidor

    for n, linea in malas:
        log(f"  aviso: no entiendo la linea {n} de {FICHERO_BASTIDORES}: '{linea[:60]}'")
    if encontrados:
        log(f"  bastidores leidos de {FICHERO_BASTIDORES}: {len(encontrados)}")
    return encontrados


def cargar_datos_extra():
    """
    Lo que el API no trae. Dos formas, se pueden usar las dos a la vez:

      1) bastidores.txt  -> una linea por coche: matricula y bastidor
      2) datos-extra.csv -> tabla con id, matricula, bastidor, garantia_meses

    Se cruza por 'id' o, si no lo hay, por matricula.
    """
    por_id, por_matricula = {}, {}

    for matricula, bastidor in cargar_bastidores_txt().items():
        por_matricula[matricula] = {"matricula": matricula, "bastidor": bastidor}

    if os.path.exists(FICHERO_EXTRA):
        with open(FICHERO_EXTRA, encoding="utf-8-sig", newline="") as f:
            muestra = f.read(2048)
            f.seek(0)
            # admite tanto comas como punto y coma, que es lo que pone Excel en espanol
            delim = ";" if muestra.count(";") > muestra.count(",") else ","
            for fila in csv.DictReader(f, delimiter=delim):
                fila = {(k or "").strip().lower(): limpiar(v) for k, v in fila.items()}
                fila["bastidor"] = re.sub(r"[^A-Za-z0-9]", "", fila.get("bastidor", "")).upper()
                if fila.get("id"):
                    por_id[fila["id"]] = fila
                if fila.get("matricula"):
                    clave = normalizar_matricula(fila["matricula"])
                    # el CSV manda sobre el txt solo si trae bastidor
                    if fila["bastidor"] or clave not in por_matricula:
                        por_matricula[clave] = fila
        log(f"  {FICHERO_EXTRA}: {len(por_id)} filas con id")

    con_bastidor = len([v for v in por_matricula.values() if v.get("bastidor")])
    con_bastidor += len([v for v in por_id.values() if v.get("bastidor")])
    log(f"  datos extra: {con_bastidor} bastidores disponibles en total")
    return por_id, por_matricula


# --------------------------------------------------------------------------
# NORMALIZACION
# --------------------------------------------------------------------------

CAMPOS_OBLIGATORIOS = ("id", "marca", "modelo", "precio", "fotos")


def normalizar(ficha, extra_id, extra_matricula):
    vid = str(ficha.get("id", "")).strip()
    matricula = normalizar_matricula(ficha.get("plate"))

    fotos = []
    for doc in ficha.get("fileDocuments") or []:
        url = limpiar(doc.get("url"))
        if url and re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I):
            fotos.append({"url": url, "principal": bool(doc.get("main"))})
    # la principal primero
    fotos.sort(key=lambda f: (not f["principal"],))

    extra = extra_id.get(vid) or extra_matricula.get(matricula) or {}

    v = {
        "id": vid,
        "matricula": matricula,
        "bastidor": extra.get("bastidor", ""),
        "marca": limpiar(ficha.get("make")),
        "modelo": limpiar(ficha.get("model")),
        "version": limpiar(ficha.get("title")),
        "anio": entero(ficha.get("year")),
        "mes": entero(ficha.get("month")),
        "km": entero(ficha.get("mileage")),
        "precio": entero(ficha.get("price")),
        "precio_anterior": entero(ficha.get("originalPrice")),
        "iva_deducible": bool(ficha.get("vatDeducible")),
        "combustible": limpiar(ficha.get("fuel")),
        "cambio": limpiar(ficha.get("gearbox")),
        "potencia_cv": entero(ficha.get("power")),
        "puertas": entero(ficha.get("doors")),
        "plazas": entero(ficha.get("seats")),
        "color": limpiar(ficha.get("color")),
        "etiqueta_dgt": limpiar(ficha.get("emissionTag")),
        "garantia_meses": entero(extra.get("garantia_meses"), 12),
        "cuota_mes": entero(ficha.get("financingPrice")),
        "meses_financiacion": entero(ficha.get("financingPeriod")),
        "reservado": entero(ficha.get("vehicleStatus")) == ESTADO_RESERVADO,
        "concesionario": limpiar(ficha.get("dealershipId")),
        "equipamiento": [limpiar(x) for x in (ficha.get("features") or []) if limpiar(x)],
        "descripcion": limpiar(ficha.get("description")),
        "url": f"{WEB}/{ficha.get('uri', '')}",
        "fotos": fotos,
    }
    # equipamiento sin duplicados, conservando el orden
    v["equipamiento"] = list(dict.fromkeys(v["equipamiento"]))
    return v


def esta_completo(v):
    faltan = [c for c in CAMPOS_OBLIGATORIOS if not v.get(c)]
    return (not faltan), faltan


# --------------------------------------------------------------------------
# SALIDAS
# --------------------------------------------------------------------------

def construir_xml(vehiculos):
    raiz = ET.Element("stock")
    raiz.set("generado", dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    raiz.set("concesionario", "MID Car Soluciones S.L.")
    raiz.set("total", str(len(vehiculos)))

    for v in vehiculos:
        n = ET.SubElement(raiz, "vehiculo")
        simples = [
            ("id", v["id"]), ("matricula", v["matricula"]), ("bastidor", v["bastidor"]),
            ("marca", v["marca"]), ("modelo", v["modelo"]), ("version", v["version"]),
            ("anio", v["anio"]), ("mes_matriculacion", v["mes"]), ("km", v["km"]),
            ("precio", v["precio"]), ("precio_anterior", v["precio_anterior"]),
            ("iva_deducible", str(v["iva_deducible"]).lower()),
            ("combustible", v["combustible"]), ("cambio", v["cambio"]),
            ("potencia_cv", v["potencia_cv"]), ("puertas", v["puertas"]),
            ("plazas", v["plazas"]), ("color", v["color"]),
            ("etiqueta_dgt", v["etiqueta_dgt"]), ("garantia_meses", v["garantia_meses"]),
            ("cuota_mes", v["cuota_mes"]), ("meses_financiacion", v["meses_financiacion"]),
            ("reservado", str(v["reservado"]).lower()),
            ("concesionario", v["concesionario"]), ("url", v["url"]),
        ]
        for etiqueta, valor in simples:
            ET.SubElement(n, etiqueta).text = str(valor)

        eq = ET.SubElement(n, "equipamiento")
        for item in v["equipamiento"]:
            ET.SubElement(eq, "item").text = item

        ET.SubElement(n, "descripcion").text = v["descripcion"]

        fotos = ET.SubElement(n, "fotos")
        for i, f in enumerate(v["fotos"], 1):
            e = ET.SubElement(fotos, "foto")
            e.set("orden", str(i))
            if f["principal"]:
                e.set("principal", "true")
            e.text = f["url"]

    crudo = ET.tostring(raiz, encoding="utf-8")
    return minidom.parseString(crudo).toprettyxml(indent="  ", encoding="utf-8")


COLUMNAS_CSV = ["id", "matricula", "marca", "modelo", "version", "anio", "km",
                "precio", "iva_deducible", "combustible", "cambio", "potencia_cv",
                "puertas", "plazas", "color", "etiqueta_dgt", "cuota_mes",
                "meses_financiacion", "reservado", "n_fotos", "url"]


def construir_csv(vehiculos, ruta):
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNAS_CSV)
        for v in vehiculos:
            fila = [v.get(c, "") for c in COLUMNAS_CSV[:-2]]
            fila.append(len(v["fotos"]))
            fila.append(v["url"])
            w.writerow(fila)


# --------------------------------------------------------------------------
# REGLA DE SEGURIDAD
# --------------------------------------------------------------------------

def stock_anterior():
    ruta = os.path.join(CARPETA_SALIDA, "resumen.json")
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return int(json.load(f).get("publicados", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            return 0
    return 0


def pasa_control(n_ahora, forzar=False):
    anterior = stock_anterior()

    if n_ahora < MINIMO_ABSOLUTO:
        motivo = (f"solo se han leido {n_ahora} vehiculos, menos del minimo "
                  f"de seguridad ({MINIMO_ABSOLUTO})")
        return (True, f"FORZADO pese a que {motivo}") if forzar else (False, motivo)

    if anterior and n_ahora < anterior * (1 - CAIDA_MAXIMA):
        caida = round((1 - n_ahora / anterior) * 100)
        motivo = (f"el stock cae de {anterior} a {n_ahora} vehiculos ({caida}% menos) "
                  f"en un solo pase")
        return (True, f"FORZADO pese a que {motivo}") if forzar else (False, motivo)

    return True, "control de seguridad superado"


# --------------------------------------------------------------------------
# PRINCIPAL
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Genera el feed de stock de MID Car")
    p.add_argument("--forzar", action="store_true",
                   help="publica aunque salte la regla de seguridad")
    p.add_argument("--sin-cache", action="store_true",
                   help="vuelve a bajar todas las fichas")
    p.add_argument("--salida", default=CARPETA_SALIDA,
                   help="carpeta donde dejar los ficheros")
    p.add_argument("--incluir-sin-vin", action="store_true",
                   help="mete en el feed de Google tambien los coches sin bastidor")
    p.add_argument("--plantilla-extra", action="store_true",
                   help="escribe una hoja con todos los coches para rellenar bastidores")
    args = p.parse_args()

    log("=" * 64)
    log("Generando feed de stock de MID Car")

    log("1. Leyendo la lista de vehiculos...")
    resumenes = bajar_resumenes()
    publicables = [r for r in resumenes
                   if entero(r.get("vehicleStatus"), -1) in ESTADOS_PUBLICABLES]
    log(f"   total en el sistema: {len(resumenes)} | publicables: {len(publicables)}")

    log("2. Leyendo las fichas completas...")
    fichas = bajar_fichas(publicables, usar_cache=not args.sin_cache)

    log("3. Normalizando y revisando datos...")
    extra_id, extra_matricula = cargar_datos_extra()
    vehiculos, descartados = [], []
    for ficha in fichas:
        v = normalizar(ficha, extra_id, extra_matricula)
        ok, faltan = esta_completo(v)
        if ok:
            vehiculos.append(v)
        else:
            descartados.append({"id": v["id"], "version": v["version"], "faltan": faltan})

    for d in descartados:
        log(f"   descartado {d['id']} ({d['version'][:50]}): falta {', '.join(d['faltan'])}")

    sin_matricula = [v["id"] for v in vehiculos if not v["matricula"]]
    sin_fotos_suficientes = [v["id"] for v in vehiculos if len(v["fotos"]) < 5]
    if sin_matricula:
        log(f"   aviso: {len(sin_matricula)} vehiculos sin matricula en el sistema")
    if sin_fotos_suficientes:
        log(f"   aviso: {len(sin_fotos_suficientes)} vehiculos con menos de 5 fotos")

    log("4. Control de seguridad...")
    ok, motivo = pasa_control(len(vehiculos), args.forzar)
    if not ok:
        log(f"   ABORTADO: {motivo}")
        log("   No se ha tocado el feed anterior. Revisa la web antes de repetir.")
        log("   Si de verdad el stock ha bajado tanto, repite con --forzar")
        return 1
    log(f"   {motivo}")

    log("5. Escribiendo ficheros...")
    os.makedirs(args.salida, exist_ok=True)
    ruta_xml = os.path.join(args.salida, "feed-maestro.xml")
    with open(ruta_xml, "wb") as f:
        f.write(construir_xml(vehiculos))
    construir_csv(vehiculos, os.path.join(args.salida, "stock.csv"))

    resumen = {
        "generado": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "publicados": len(vehiculos),
        "total_sistema": len(resumenes),
        "descartados": len(descartados),
        "sin_matricula": len(sin_matricula),
        "con_pocas_fotos": len(sin_fotos_suficientes),
        "precio_medio": round(sum(v["precio"] for v in vehiculos) / len(vehiculos)) if vehiculos else 0,
        "fotos_totales": sum(len(v["fotos"]) for v in vehiculos),
    }
    with open(os.path.join(args.salida, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)

    tam = os.path.getsize(ruta_xml) / 1024
    log(f"   {ruta_xml} ({tam:.0f} KB) con {len(vehiculos)} vehiculos")
    log(f"   precio medio {resumen['precio_medio']} EUR | {resumen['fotos_totales']} fotos")

    log("6. Traduciendo a los formatos de cada destino...")

    g = adaptadores.google_vehicle_ads(vehiculos, args.salida, STORE_CODE,
                                       incluir_sin_vin=args.incluir_sin_vin)
    log(f"   Google Vehicle Ads: {g['publicados']} de {len(vehiculos)} vehiculos")
    if g["sin_vin"]:
        log(f"   ATENCION: {g['sin_vin']} vehiculos sin bastidor (VIN), que Google exige.")
        log(f"   Los tienes listados en {g['ruta_pendientes']} para rellenarlos.")
    if g["sin_fecha"]:
        log(f"   aviso: {g['sin_fecha']} sin mes de matriculacion (Google lo pide en Espana)")
    if g["sin_foto"]:
        log(f"   aviso: {g['sin_foto']} sin ninguna foto")

    ia = adaptadores.stock_para_ia(vehiculos, args.salida)
    log(f"   Stock para IA: {ia['total']} vehiculos en {ia['json']} y {ia['txt']}")

    if args.plantilla_extra:
        ruta = adaptadores.plantilla_datos_extra(vehiculos, FICHERO_EXTRA + ".plantilla.csv")
        log(f"   Plantilla de bastidores escrita en {ruta}")

    resumen["google_publicados"] = g["publicados"]
    resumen["google_sin_vin"] = g["sin_vin"]
    with open(os.path.join(args.salida, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)

    log("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
