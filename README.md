# Agente conversacional — Observatorio Sociolaboral de Euskadi

Agente de IA que responde preguntas en lenguaje natural sobre los datos del
**Observatorio Sociolaboral con perspectiva de género en Euskadi**, traduciendo
esas preguntas a consultas SQL (patrón *Text-to-SQL*) sobre una base de datos
Azure SQL.

Proyecto desarrollado como parte del TFG en el Aula Open Data Bilbao-Bizkaia,
con datos abiertos de Eustat e INE.

## Arquitectura

```
   Usuario
     │  pregunta en lenguaje natural
     ▼
   Agente (Text-to-SQL)  ──►  Modelo de IA  (genera la consulta SQL)
     │
     ▼
   Azure SQL Database    ──►  ejecuta la consulta SELECT
     │
     ▼
   Modelo de IA          ──►  redacta la respuesta en lenguaje natural
     │
     ▼
   Usuario
```

## Proveedor de IA intercambiable

El proveedor de IA se configura por variables de entorno y se puede cambiar
**sin tocar el código**, solo editando el archivo `.env`:

- **GitHub Models** — gratis para estudiantes con el GitHub Student Developer
  Pack. Límites de uso pensados para desarrollo y pruebas. Recomendado para
  construir y depurar el agente.
- **OpenAI** — API de pago (coste de céntimos al mes para uso de TFG).
  Recomendado para la presentación, por estabilidad.
- **Azure OpenAI** — compatible también, si se dispone de acceso.

Los tres usan el SDK de OpenAI, que es compatible entre ellos: solo cambian la
URL base, la clave y el nombre del modelo.

## Instalación

```bash
pip install -r requirements.txt
cp .env.example .env      # luego edita .env con tus credenciales
```

Requiere el *ODBC Driver 18 for SQL Server* instalado en el sistema para
conectar con Azure SQL.

## Uso

El agente tiene dos interfaces que comparten la misma lógica:

Interfaz web (recomendada para la demostración):

```bash
streamlit run web.py
```

Se abre en el navegador. Permite preguntar en lenguaje natural y muestra la
respuesta, la consulta SQL generada y la tabla de datos.

Interfaz de línea de comandos:

```bash
python main.py
```

Escribe preguntas en lenguaje natural; el agente muestra la consulta SQL
generada y la respuesta. Escribe `salir` para terminar.

## Estructura del proyecto

| Archivo          | Función                                                |
|------------------|--------------------------------------------------------|
| `config.py`      | Configuración: lee el `.env`, define el proveedor de IA |
| `database.py`    | Conexión a Azure SQL, esquema, diccionario, consultas   |
| `agent.py`       | Lógica Text-to-SQL (memoria, territorios, sexo)         |
| `main.py`        | Interfaz de línea de comandos                           |
| `web.py`         | Interfaz web con Streamlit                              |
| `cargar_datos.py`| Carga inicial del Excel a la base de datos              |
| `.env.example`   | Plantilla de variables de entorno                       |

## Seguridad

- El agente solo ejecuta consultas `SELECT`; rechaza cualquier instrucción de
  escritura o modificación del esquema.
- El archivo `.env` con las credenciales está excluido del control de versiones.

## Tecnologías

Python · Azure SQL Database · API de modelos de lenguaje (GitHub Models / OpenAI)
