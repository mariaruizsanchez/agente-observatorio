"""
Interfaz web del agente del Observatorio Sociolaboral.

Es una capa de presentación: toda la lógica vive en agent.py y database.py,
que NO se modifican. Esta web simplemente los usa.

El diseño sigue una paleta sobria (morados, grises, blanco y negro), acorde
con el informe de Power BI del Observatorio, y un estilo minimalista.

Para ejecutarla:
    streamlit run web.py
"""

import streamlit as st
import pandas as pd

import config
import database
import agent


# --- Configuración de la página --------------------------------------------
st.set_page_config(
    page_title="Observatorio Sociolaboral - Euskadi",
    layout="centered",
)


# --- Estilo visual (paleta morada, minimalista) ----------------------------
ESTILO = """
<style>
html, body, [class*="css"] { color: #1A1A1A; }

/* Oculta el menú, el pie y la barra superior de Streamlit */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}

/* Título principal */
h1 { color: #3D3260 !important; font-weight: 700 !important; }

/* Subtítulos y encabezados de la barra lateral */
h2, h3 { color: #5B4B8A !important; }

/* Barra lateral con fondo gris muy claro */
section[data-testid="stSidebar"] {
    background-color: #F4F3F7;
    border-right: 1px solid #E0DEE8;
}

/* Mensajes del AGENTE: fondo suave y borde discreto */
div[data-testid="stChatMessage"] {
    background-color: #F4F3F7;
    border: 1px solid #E0DEE8;
    border-radius: 10px;
    padding: 4px 14px;
}

/* Mensajes del USUARIO: sin fondo, sin borde, sin avatar.
   El mensaje del usuario es el que tiene la clase de rol "user". */
div[data-testid="stChatMessage"]:has(div[data-testid="stChatMessageAvatarUser"]) {
    background-color: transparent;
    border: none;
    padding: 0 14px;
}
/* Oculta el hueco del avatar en el mensaje del usuario */
div[data-testid="stChatMessageAvatarUser"] {
    display: none;
}

/* Botón: morado principal */
div.stButton > button {
    background-color: #5B4B8A;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    font-weight: 600;
}
div.stButton > button:hover {
    background-color: #3D3260;
    color: #FFFFFF;
}

/* Caja de texto de la pregunta: borde morado, nunca rojo.
   Se fuerza en todos los estados para sobreescribir el color por defecto. */
div[data-testid="stChatInput"] {
    border: 1px solid #5B4B8A !important;
    border-radius: 8px !important;
}
div[data-testid="stChatInput"]:focus-within {
    border: 1px solid #5B4B8A !important;
    box-shadow: 0 0 0 1px #5B4B8A !important;
}
div[data-testid="stChatInput"] > div {
    border-color: #5B4B8A !important;
}
div[data-testid="stChatInput"] textarea {
    border: none !important;
}

/* Apartado desplegable (el del SQL) */
div[data-testid="stExpander"] {
    border: 1px solid #E0DEE8;
    border-radius: 8px;
}
</style>
"""
st.markdown(ESTILO, unsafe_allow_html=True)


# --- Carga inicial (esquema, diccionario, territorios) ----------------------
@st.cache_resource
def cargar_contexto():
    config.validar()
    esquema = database.obtener_esquema()
    diccionario = database.obtener_diccionario()
    territorios, mapa = database.obtener_territorios()
    return esquema, diccionario, territorios, mapa


# --- Cabecera ---------------------------------------------------------------
st.title("Observatorio Sociolaboral de Euskadi")
st.caption("Agente conversacional con perspectiva de género · "
           "Pregunta en lenguaje natural sobre los datos")

try:
    esquema, diccionario, territorios, mapa = cargar_contexto()
except Exception as e:
    st.error(f"No se pudo iniciar el agente: {e}")
    st.stop()


# --- Historial de la conversación -------------------------------------------
if "mensajes" not in st.session_state:
    st.session_state.mensajes = []
if "historial" not in st.session_state:
    st.session_state.historial = []


def mostrar_pregunta(texto):
    """Muestra un mensaje del usuario: sin avatar, en texto simple."""
    # No se usa st.chat_message para el usuario, para que no tenga avatar
    # ni recuadro. Se muestra como texto resaltado.
    st.markdown(f"**Tu pregunta:** {texto}")


# --- Mostrar los mensajes anteriores ----------------------------------------
for msg in st.session_state.mensajes:
    if msg["rol"] == "user":
        mostrar_pregunta(msg["texto"])
    else:
        # El agente sí lleva avatar de robot.
        with st.chat_message("assistant", avatar="\U0001F916"):
            st.markdown(msg["texto"])
            if msg.get("sql"):
                with st.expander("Ver consulta SQL generada"):
                    st.code(msg["sql"], language="sql")
            if msg.get("tabla") is not None:
                st.dataframe(msg["tabla"], use_container_width=True)


# --- Entrada del usuario ----------------------------------------------------
pregunta = st.chat_input("Escribe tu pregunta...")

if pregunta:
    mostrar_pregunta(pregunta)
    st.session_state.mensajes.append({"rol": "user", "texto": pregunta})

    with st.chat_message("assistant", avatar="\U0001F916"):
        with st.spinner("Consultando..."):
            resultado = agent.responder(
                pregunta, esquema, diccionario, territorios,
                mapa, st.session_state.historial,
            )
        st.session_state.historial = resultado["historial"]

        st.markdown(resultado["respuesta"])

        tabla = None
        if resultado.get("filas"):
            tabla = pd.DataFrame(resultado["filas"],
                                 columns=resultado["cabeceras"])

        if resultado.get("sql"):
            with st.expander("Ver consulta SQL generada"):
                st.code(resultado["sql"], language="sql")
        if tabla is not None:
            st.dataframe(tabla, use_container_width=True)

    st.session_state.mensajes.append({
        "rol": "assistant",
        "texto": resultado["respuesta"],
        "sql": resultado.get("sql"),
        "tabla": tabla,
    })


# --- Barra lateral con ayuda ------------------------------------------------
with st.sidebar:
    st.header("Sobre el agente")
    st.write("Este asistente traduce preguntas en lenguaje natural a "
             "consultas SQL sobre la base de datos del Observatorio.")
    st.subheader("Ejemplos de preguntas")
    st.write("- Evolución de la tasa de paro por año en Bizkaia")
    st.write("- Salario medio por año")
    st.write("- Número de nacimientos en Gipuzkoa")
    if st.button("Reiniciar conversación"):
        st.session_state.mensajes = []
        st.session_state.historial = []
        st.rerun()
