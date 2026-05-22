"""
Interfaz web del agente del Observatorio Sociolaboral.

Es una capa de presentación: toda la lógica vive en agent.py y database.py,
que NO se modifican. Esta web simplemente los usa.

Para ejecutarla:
    streamlit run web.py

Se abrirá automáticamente en el navegador.
"""

import streamlit as st
import pandas as pd

import config
import database
import agent


# --- Configuración de la página -------------------------------------------
st.set_page_config(
    page_title="Observatorio Sociolaboral — Euskadi",
    page_icon="📊",
    layout="centered",
)


# --- Carga inicial (esquema, diccionario, territorios) ----------------------
# @st.cache_resource hace que esto se ejecute UNA sola vez, no en cada
# interacción: así la web es rápida.
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
# st.session_state guarda datos entre interacciones del usuario.
if "mensajes" not in st.session_state:
    st.session_state.mensajes = []      # lo que se muestra en pantalla
if "historial" not in st.session_state:
    st.session_state.historial = []     # memoria que se pasa al agente


# --- Mostrar los mensajes anteriores ----------------------------------------
for msg in st.session_state.mensajes:
    with st.chat_message(msg["rol"]):
        st.markdown(msg["texto"])
        # Si el mensaje del agente traía SQL y tabla, los mostramos.
        if msg.get("sql"):
            with st.expander("Ver consulta SQL generada"):
                st.code(msg["sql"], language="sql")
        if msg.get("tabla") is not None:
            st.dataframe(msg["tabla"], use_container_width=True)


# --- Entrada del usuario ----------------------------------------------------
pregunta = st.chat_input("Escribe tu pregunta...")

if pregunta:
    # Mostramos la pregunta del usuario.
    with st.chat_message("user"):
        st.markdown(pregunta)
    st.session_state.mensajes.append({"rol": "user", "texto": pregunta})

    # Llamamos al agente.
    with st.chat_message("assistant"):
        with st.spinner("Consultando..."):
            resultado = agent.responder(
                pregunta, esquema, diccionario, territorios,
                mapa, st.session_state.historial,
            )
        # Actualizamos la memoria de la conversación.
        st.session_state.historial = resultado["historial"]

        # Mostramos la respuesta en lenguaje natural.
        st.markdown(resultado["respuesta"])

        # Preparamos la tabla de datos (si la hay).
        tabla = None
        if resultado.get("filas"):
            tabla = pd.DataFrame(resultado["filas"],
                                 columns=resultado["cabeceras"])

        # Mostramos el SQL y la tabla.
        if resultado.get("sql"):
            with st.expander("Ver consulta SQL generada"):
                st.code(resultado["sql"], language="sql")
        if tabla is not None:
            st.dataframe(tabla, use_container_width=True)

    # Guardamos el mensaje del agente para que siga visible.
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
