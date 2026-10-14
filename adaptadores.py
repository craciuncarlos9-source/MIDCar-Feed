#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Traductores del feed maestro a los formatos que pide cada destino.

De momento:
  - Google Vehicle Ads (Merchant Center)  -> TSV
  - Stock para una IA (ChatGPT y similares) -> JSON + texto plano
"""

import csv
import json
import os
import re

# --------------------------------------------------------------------------
# GOOGLE VEHICLE ADS
# --------------------------------------------------------------------------

# Encabezados EXACTOS que exige Google. No se tocan.
COLUMNAS_GOOGLE = [
    "id", "vin", "title", "description", "link", "image_link",
    "additional_image_link", "price", "condition", "brand", "model", "year",
    "mileage", "color", "date_first_registered", "engine",
    "store_code", "vehicle_fulfillment",
]

MAX_FOTOS_EXTRA = 10


def _motor(v):
    """Traduce el combustible a los valores que admite Google."""
    texto = f"{v.get('version','')} {v.get('descripcion','')}".lower()
    comb = (v.get("combustible") or "").lower()

    if "enchufable" in texto or "plug" in texto or "e-hybrid" in texto:
        return "plug-in_hybrid"
    if comb.startswith("el"):                       # Electrico
        return "electric"
    if comb.startswith("di"):                       # Diesel
        return "diesel"
    if comb.startswith("gasolina"):
        return "petrol"
    if "hibrido" in comb or "híbrido" in comb:
        return "hybrid"
    if comb == "gas":
        # En el stock de MID Car "Gas" es GNC o GLP segun el modelo
        if re.search(r"\bgnc\b|\btgi\b|metano|cng", texto):
            return "natural_gas"
        if re.search(r"\bglp\b|\blpg\b", texto):
            return "lpg"
        return "other"
    return "other"


def _fecha_matriculacion(v):
    anio, mes = v.get("anio"), v.get("mes")
    if not anio:
        return ""
    if not mes or not (1 <= int(mes) <= 12):
        return ""
    return f"{int(anio):04d}-{int(mes):02d}"


def _fila_google(v, store_code):
    fotos = [f["url"] for f in v.get("fotos", [])]
    return {
        "id": v["id"],
        "vin": v.get("bastidor", ""),
        "title": (v.get("version") or "")[:150],
        "description": (v.get("descripcion") or "")[:5000],
        "link": v.get("url", ""),
        "image_link": fotos[0] if fotos else "",
        "additional_image_link": ",".join(fotos[1:1 + MAX_FOTOS_EXTRA]),
        "price": f"{int(v.get('precio', 0))}.00 EUR",
        "condition": "Used",
        "brand": v.get("marca", ""),
        "model": v.get("modelo", ""),
        "year": str(v.get("anio", "")),
        "mileage": f"{int(v.get('km', 0))} KM",
        "color": v.get("color", ""),
        "date_first_registered": _fecha_matriculacion(v),
        "engine": _motor(v),
        "store_code": store_code,
        "vehicle_fulfillment": "in_store",
    }


def google_vehicle_ads(vehiculos, carpeta, store_code, incluir_sin_vin=False):
    """
    Escribe el feed para Merchant Center. Devuelve un resumen con lo que ha
    quedado fuera y por que, para poder avisar.
    """
    listos, sin_vin, sin_fecha, sin_foto = [], [], [], []

    for v in vehiculos:
        fila = _fila_google(v, store_code)
        problemas = []
        if not fila["vin"]:
            problemas.append("vin")
            sin_vin.append(v)
        if not fila["date_first_registered"]:
            problemas.append("fecha")
            sin_fecha.append(v)
        if not fila["image_link"]:
            problemas.append("foto")
            sin_foto.append(v)

        bloqueantes = [p for p in problemas if p != "vin" or not incluir_sin_vin]
        if not bloqueantes:
            listos.append(fila)

    ruta = os.path.join(carpeta, "google-vehicle-ads.tsv")
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_GOOGLE, delimiter="\t",
                           extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for fila in listos:
            # los saltos de linea rompen un TSV
            w.writerow({k: str(val).replace("\n", " ").replace("\t", " ")
                        for k, val in fila.items()})

    # Lista de los que faltan por completar, para que sea facil arreglarlo
    ruta_pendientes = os.path.join(carpeta, "pendientes-bastidor.csv")
    if sin_vin:
        with open(ruta_pendientes, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "matricula", "bastidor", "garantia_meses", "vehiculo"])
            for v in sin_vin:
                w.writerow([v["id"], v.get("matricula", ""), "", "", v.get("version", "")])
    elif os.path.exists(ruta_pendientes):
        os.remove(ruta_pendientes)

    return {
        "publicados": len(listos),
        "sin_vin": len(sin_vin),
        "sin_fecha": len(sin_fecha),
        "sin_foto": len(sin_foto),
        "ruta": ruta,
        "ruta_pendientes": ruta_pendientes if sin_vin else None,
    }


# --------------------------------------------------------------------------
# STOCK PARA UNA IA
# --------------------------------------------------------------------------

def stock_para_ia(vehiculos, carpeta):
    """
    Dos formatos del mismo stock:
      stock-ia.json -> para que un programa lo lea
      stock-ia.txt  -> para pegarlo en el prompt o subirlo como archivo
    """
    compacto = []
    for v in vehiculos:
        compacto.append({
            "id": v["id"],
            "vehiculo": v.get("version", ""),
            "marca": v.get("marca", ""),
            "modelo": v.get("modelo", ""),
            "anio": v.get("anio"),
            "km": v.get("km"),
            "precio_eur": v.get("precio"),
            "cuota_mes_eur": v.get("cuota_mes"),
            "meses_financiacion": v.get("meses_financiacion"),
            "combustible": v.get("combustible", ""),
            "cambio": v.get("cambio", ""),
            "potencia_cv": v.get("potencia_cv"),
            "etiqueta_dgt": v.get("etiqueta_dgt", ""),
            "plazas": v.get("plazas"),
            "puertas": v.get("puertas"),
            "color": v.get("color", ""),
            "iva_deducible": v.get("iva_deducible"),
            "garantia_meses": v.get("garantia_meses"),
            "equipamiento": v.get("equipamiento", []),
            "url": v.get("url", ""),
            "foto": (v.get("fotos") or [{}])[0].get("url", ""),
        })

    ruta_json = os.path.join(carpeta, "stock-ia.json")
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump({"actualizado": True, "total": len(compacto), "vehiculos": compacto},
                  f, ensure_ascii=False, indent=1)

    euros = lambda n: f"{int(n or 0):,}".replace(",", ".")
    lineas = []
    for v in compacto:
        partes = [
            v["vehiculo"], str(v["anio"]), f"{euros(v['km'])} km",
            f"{v['potencia_cv']} CV", v["combustible"], v["cambio"],
            f"etiqueta {v['etiqueta_dgt'] or 'sin'}", f"{euros(v['precio_eur'])} EUR",
        ]
        if v["cuota_mes_eur"]:
            partes.append(f"desde {v['cuota_mes_eur']} EUR/mes a {v['meses_financiacion']} meses")
        if v["iva_deducible"]:
            partes.append("IVA deducible")
        partes.append(v["url"])
        lineas.append("- " + " | ".join(str(p) for p in partes if p))

    ruta_txt = os.path.join(carpeta, "stock-ia.txt")
    with open(ruta_txt, "w", encoding="utf-8") as f:
        f.write(f"STOCK DISPONIBLE DE MID CAR ({len(compacto)} vehiculos)\n\n")
        f.write("\n".join(lineas) + "\n")

    return {"total": len(compacto), "json": ruta_json, "txt": ruta_txt}


# --------------------------------------------------------------------------
# PLANTILLA PARA RELLENAR BASTIDORES
# --------------------------------------------------------------------------

def plantilla_datos_extra(vehiculos, ruta):
    """Hoja con un renglon por vehiculo para ir pegando los bastidores."""
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "matricula", "bastidor", "garantia_meses", "vehiculo"])
        for v in vehiculos:
            w.writerow([v["id"], v.get("matricula", ""), v.get("bastidor", ""),
                        v.get("garantia_meses", ""), v.get("version", "")])
    return ruta
