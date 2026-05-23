"""
Configuracion centralizada del agente.

La configuracion (proveedor de IA y base de datos) se obtiene de dos posibles
fuentes, segun donde se ejecute la aplicacion:

  - En LOCAL (el ordenador): de las variables de entorno del archivo .env.
  - En LA NUBE (Streamlit Community Cloud): de los "secrets" de Streamlit,
    que se configuran en el panel de la aplicacion desplegada.

El codigo intenta primero los secrets de Streamlit; si no existen, recurre
al archivo .env. De este modo, el mismo codigo funciona en ambos entornos
sin necesidad de modificarlo.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # carga las variables del archivo .env (entorno local)


def _leer(clave, por_defecto=""):
    """
    Devuelve el valor de una clave de configuracion.

    Orden de busqueda:
      1. Secrets de Streamlit (cuando la app corre en Streamlit Cloud).
      2. Variables de entorno / archivo .env (cuando corre en local).
      3. Valor por defecto.
    """
    try:
        import streamlit as st
        if clave in st.secrets:
            return st.secrets[clave]
    except Exception:
        pass
    return os.getenv(clave, por_defecto)


# ---------------------------------------------------------------------------
# PROVEEDOR DE IA
# ---------------------------------------------------------------------------
# GitHub Models, OpenAI y Azure OpenAI son compatibles con el SDK de OpenAI:
# solo cambian la URL base, la clave y el nombre del modelo.
# ---------------------------------------------------------------------------

AI_BASE_URL = _leer("AI_BASE_URL", "https://models.inference.ai.azure.com")
AI_API_KEY = _leer("AI_API_KEY", "")
AI_MODEL = _leer("AI_MODEL", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# BASE DE DATOS  (Azure SQL Database)
# ---------------------------------------------------------------------------
# La conexion se realiza con la libreria pymssql, que necesita los datos por
# separado (servidor, usuario, contrase\u00f1a y base de datos).
#
# En el archivo .env (local) o en los secrets (nube), se definen asi:
#   DB_SERVER=srv-observatorio-mrs.database.windows.net
#   DB_USER=adminsql
#   DB_PASSWORD=tu_contrase\u00f1a
#   DB_NAME=db-observatorio
# ---------------------------------------------------------------------------

DB_SERVER = _leer("DB_SERVER", "")
DB_USER = _leer("DB_USER", "")
DB_PASSWORD = _leer("DB_PASSWORD", "")
DB_NAME = _leer("DB_NAME", "")


def validar():
    """Comprueba que la configuracion minima esta presente y avisa con claridad."""
    faltan = []
    if not AI_API_KEY:
        faltan.append("AI_API_KEY")
    for nombre, valor in [("DB_SERVER", DB_SERVER), ("DB_USER", DB_USER),
                          ("DB_PASSWORD", DB_PASSWORD), ("DB_NAME", DB_NAME)]:
        if not valor:
            faltan.append(nombre)
    if faltan:
        raise SystemExit(
            "Falta configuracion: " + ", ".join(faltan) +
            "\nEn local: revisa el archivo .env. "
            "En Streamlit Cloud: revisa los secrets de la aplicacion."
        )
