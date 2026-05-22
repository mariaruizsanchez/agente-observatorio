"""
Interfaz de línea de comandos del agente del Observatorio Sociolaboral.

Uso:
    python main.py

Escribe tus preguntas en lenguaje natural. Escribe 'salir' para terminar.

Esta es la interfaz mínima para validar que el Text-to-SQL funciona.
Una vez confirmado, se puede añadir una interfaz web (Streamlit) reutilizando
el módulo agent.py tal cual.
"""

import config
import database
import agent


def main():
    config.validar()  # comprueba que el .env está bien antes de empezar

    print("Agente del Observatorio Sociolaboral — Euskadi")
    print("Proveedor de IA:", config.AI_BASE_URL)
    print("Cargando esquema de la base de datos...\n")

    try:
        esquema = database.obtener_esquema()
    except Exception as e:
        raise SystemExit(f"No se pudo conectar a la base de datos: {e}")

    # El diccionario es opcional: si el Excel no está, el agente sigue
    # funcionando solo con el esquema (aunque con menos precisión).
    diccionario = database.obtener_diccionario()
    if diccionario:
        print("Diccionario de datos cargado.")
    else:
        print("Aviso: no se encontró el diccionario; el agente funcionará "
              "solo con el esquema.")

    # Territorios reales de cada tabla: para saber cuándo pedir aclaración.
    territorios, mapa_territorios = database.obtener_territorios()
    print("Territorios de cada tabla cargados.")

    print("Esquema cargado. Ya puedes preguntar (escribe 'salir' para terminar).\n")

    historial = []  # memoria de la conversación en curso
    while True:
        pregunta = input("Tú > ").strip()
        if pregunta.lower() in ("salir", "exit", "quit", ""):
            print("Hasta luego.")
            break

        resultado = agent.responder(pregunta, esquema, diccionario,
                                    territorios, mapa_territorios, historial)
        historial = resultado["historial"]  # se actualiza para la siguiente

        if resultado["sql"] is None:
            # El agente necesita que concretes la pregunta.
            print("\n[El agente necesita una aclaración]")
            print(resultado["respuesta"])
            print()
        else:
            print("\n[SQL generado]")
            print(resultado["sql"])
            print("\n[Respuesta]")
            print(resultado["respuesta"])
            print()


if __name__ == "__main__":
    main()
