"""
Configuración centralizada del agente.

TODA la configuración del proveedor de IA y de la base de datos vive aquí
y se lee desde variables de entorno (archivo .env). Esto es lo que permite
cambiar de GitHub Models a OpenAI o Anthropic SIN TOCAR el resto del código:
solo editas el .env.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # carga las variables del archivo .env


# ---------------------------------------------------------------------------
# PROVEEDOR DE IA
# ---------------------------------------------------------------------------
# GitHub Models, OpenAI y Azure OpenAI son todos compatibles con el SDK de
# OpenAI: solo cambian la URL base, la clave y el nombre del modelo.
#
# Para usar cada proveedor, copia .env.example a .env y rellena segun:
#
#   GitHub Models  (gratis para estudiantes, ideal para DESARROLLO)
#     AI_BASE_URL=https://models.inference.ai.azure.com
#     AI_API_KEY=<tu token de GitHub con permiso de "models">
#     AI_MODEL=gpt-4o-mini
#
#   OpenAI directo (de pago, centimos/mes, ideal para la PRESENTACION)
#     AI_BASE_URL=https://api.openai.com/v1
#     AI_API_KEY=<tu clave de OpenAI>
#     AI_MODEL=gpt-4o-mini
#
#   Azure OpenAI (si algun dia se desbloquea tu acceso)
#     AI_BASE_URL=https://<tu-recurso>.openai.azure.com/openai/deployments/<deployment>
#     AI_API_KEY=<tu clave de Azure>
#     AI_MODEL=<nombre de tu deployment>
# ---------------------------------------------------------------------------

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://models.inference.ai.azure.com")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# BASE DE DATOS  (Azure SQL Database / SQL Server)
# ---------------------------------------------------------------------------
# La cadena de conexion ODBC completa. Ejemplo para Azure SQL:
#
#   DB_CONNECTION_STRING=Driver={ODBC Driver 18 for SQL Server};
#       Server=tcp:<tu-servidor>.database.windows.net,1433;
#       Database=<tu-bbdd>;Uid=<usuario>;Pwd=<contraseña>;
#       Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;
# ---------------------------------------------------------------------------

DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING", "")


def validar():
    """Comprueba que la configuración mínima está presente y avisa con claridad."""
    faltan = []
    if not AI_API_KEY:
        faltan.append("AI_API_KEY")
    if not DB_CONNECTION_STRING:
        faltan.append("DB_CONNECTION_STRING")
    if faltan:
        raise SystemExit(
            "Falta configuración en el archivo .env: " + ", ".join(faltan) +
            "\nCopia .env.example a .env y rellena los valores."
        )
