"""
agent_runner.py
===============
Motor de conversación: integra el modelo LLM con las herramientas de la API.

Responsabilidad
---------------
Este módulo implementa el bucle de razonamiento del agente de IA:
  1. Mantiene el historial de mensajes de cada conversación en memoria.
  2. Envía los mensajes al modelo grok-4.3 (Azure OpenAI) con las
     definiciones de herramientas (function calling).
  3. Cuando el modelo solicita ejecutar una herramienta, llama a api_client
     con el token JWT del usuario y devuelve el resultado al modelo.
  4. Repite hasta que el modelo genera una respuesta final en texto.

Por qué Chat Completions y no Agents API
-----------------------------------------
Azure AI Foundry ofrece una Agents API (SDK azure-ai-agents) que gestiona
automáticamente el bucle de herramientas. Sin embargo, esa API solo es
compatible con modelos GPT-* de OpenAI. El modelo disponible en esta
suscripción es grok-4.3 (xAI), que no es compatible con la Agents API.

Por ello usamos directamente la API de Chat Completions con 'tool_choice=auto',
que sí es compatible con grok-4.3, e implementamos el bucle nosotros.

Gestión del contexto (hilos en memoria)
----------------------------------------
Los hilos de conversación se guardan en el diccionario _threads:
    { thread_id (UUID) -> [lista de mensajes] }

Esto es adecuado para una demo o despliegue de instancia única. En producción
con múltiples instancias o réplicas se necesitaría almacenamiento externo
(Redis, base de datos) para compartir el estado entre instancias.

Token de usuario vs. credenciales del servidor
-----------------------------------------------
Las llamadas a la API REST se realizan con el token JWT del usuario
(obtenido de la sesión Flask), NO con credenciales del servidor. Esto
garantiza que cada empleado solo puede acceder a sus propios registros,
ya que la API valida el token y extrae el UPN del empleado.

Variables de entorno necesarias
--------------------------------
  FOUNDRY_API_KEY - API key del recurso Azure AI Foundry (ws-reg-horas-resource).
                    Se usa para autenticar las llamadas al modelo grok-4.3.
"""

import json
import os
from datetime import datetime
from openai import AzureOpenAI
import api_client

# Endpoint del recurso Azure OpenAI asociado al proyecto de AI Foundry.
ENDPOINT = "https://ws-reg-horas-resource.openai.azure.com"

# Nombre del deployment del modelo en Azure AI Foundry.
# El modelo grok-4.3 (xAI) es el único compatible con esta suscripción MCT;
# los modelos GPT-* de OpenAI no están disponibles por restricciones de cuota.
DEPLOYMENT = "grok-4.3"

# ── Prompt del sistema ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un asistente de registro horario para empleados. Tu tarea es ayudar a gestionar fichajes de entrada y salida.

Cuando el empleado quiera:
- Registrar su entrada ahora → llama a fichar_entrada
- Registrar su salida ahora → llama a fichar_salida
- Corregir una entrada pasada → pide la hora exacta y el motivo, luego llama a corregir_entrada
- Corregir una salida pasada → pide la hora exacta y el motivo, luego llama a corregir_salida
- Ver sus horas o registros → llama a consultar_horas

Al mostrar el resultado de consultar_horas, presenta los registros agrupados por día con este formato:
📅 DD/MM/YYYY
  • Entrada: HH:MM:SS  Salida: HH:MM:SS  Tiempo: HH:MM
(repite para cada registro del día)
Total: HH:MM:SS

Responde siempre en español, de forma amable y concisa. Confirma el resultado de cada operación al usuario."""

# ── Definición de herramientas (function calling) ─────────────────────────────
#
# Estas definiciones siguen el esquema JSON de OpenAI function calling.
# El modelo las recibe en cada llamada y decide qué función invocar según
# el contexto de la conversación. Los nombres deben coincidir exactamente
# con las funciones implementadas en _execute_tool().

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fichar_entrada",
            "description": "Registra la entrada del empleado con el timestamp actual.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fichar_salida",
            "description": "Registra la salida del empleado con el timestamp actual.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corregir_entrada",
            "description": "Registra una entrada con una hora pasada específica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "momento_entrada": {
                        "type": "string",
                        "description": "Fecha y hora en formato 'aaaa-mm-dd hh:mm:ss'.",
                    },
                    "comentario_justificacion": {
                        "type": "string",
                        "description": "Motivo de la corrección.",
                    },
                },
                "required": ["momento_entrada", "comentario_justificacion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corregir_salida",
            "description": "Registra una salida con una hora pasada específica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "momento_salida": {
                        "type": "string",
                        "description": "Fecha y hora en formato 'aaaa-mm-dd hh:mm:ss'.",
                    },
                    "comentario_justificacion": {
                        "type": "string",
                        "description": "Motivo de la corrección.",
                    },
                },
                "required": ["momento_salida", "comentario_justificacion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_horas",
            "description": "Consulta los registros y horas trabajadas en un rango de fechas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha_desde": {
                        "type": "string",
                        "description": "Fecha inicio en formato 'aaaa-mm-dd'. Por defecto hoy.",
                    },
                    "fecha_hasta": {
                        "type": "string",
                        "description": "Fecha fin en formato 'aaaa-mm-dd'. Por defecto hoy.",
                    },
                },
                "required": [],
            },
        },
    },
]

# ── Almacén de conversaciones en memoria ──────────────────────────────────────
#
# Diccionario que mapea thread_id (UUID string) a la lista de mensajes
# del historial de esa conversación. Incluye el system prompt al inicio.
# Limitación: se pierde al reiniciar el servidor. Aceptable para una demo.
_threads: dict[str, list] = {}


# ── Cliente Azure OpenAI ──────────────────────────────────────────────────────

def _get_client() -> AzureOpenAI:
    """
    Crea y devuelve un cliente Azure OpenAI autenticado con API key.

    Se usa API key en lugar de credenciales de identidad (ClientSecretCredential)
    porque el endpoint ws-reg-horas-resource.openai.azure.com requiere la clave
    directamente. La clave se lee del entorno (FOUNDRY_API_KEY en .env local
    o App Setting en Azure App Service).
    """
    return AzureOpenAI(
        api_key=os.getenv("FOUNDRY_API_KEY"),
        azure_endpoint=ENDPOINT,
        api_version="2025-01-01-preview",
    )


# ── Formateo de timestamps y resultados ───────────────────────────────────────

def _ts_local(ts_str: str | None) -> str | None:
    """
    Convierte un timestamp ISO 8601 (UTC) a hora local +02:00 con formato legible.

    Por qué es necesario: PostgreSQL almacena los timestamps en UTC internamente,
    aunque la API los escriba en hora local (+02:00). Al leerlos de vuelta,
    SQLAlchemy los devuelve como UTC. Esta función los convierte antes de
    mostrarlos al usuario.

    Parámetros
    ----------
    ts_str : str | None
        Timestamp en formato ISO 8601, p.ej. '2026-06-08T11:30:00+00:00' o
        '2026-06-08T11:30:00Z'. Acepta None (registros pendientes sin salida).

    Retorna
    -------
    str | None
        Timestamp formateado como 'dd/mm/aaaa HH:MM:SS' en zona +02:00,
        o None si la entrada es None.
    """
    if not ts_str:
        return None
    from datetime import timezone, timedelta
    TZ_LOCAL = timezone(timedelta(hours=2))
    # Python < 3.11 no acepta 'Z' como sufijo UTC; normalizamos a '+00:00'.
    ts_str_clean = ts_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_str_clean)
        if dt.tzinfo is None:
            # Si llega sin timezone, asumimos UTC (comportamiento por defecto de PG).
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(TZ_LOCAL)
        return local.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        # Si el parsing falla, devolvemos el string original sin convertir.
        return ts_str


def _segundos_a_hhmm(segundos: int | None) -> str | None:
    """
    Convierte segundos enteros a formato 'HH:MM' legible.

    Retorna None si segundos es None (registro sin salida, tiempo no calculado).
    """
    if segundos is None:
        return None
    h = segundos // 3600
    m = (segundos % 3600) // 60
    return f"{h:02d}:{m:02d}"


def _formatear_consulta(data: dict) -> dict:
    """
    Post-procesa la respuesta de consultar_horas para el modelo LLM.

    Transforma la estructura plana de registros en una agrupación por fecha,
    con timestamps convertidos a hora local (+02:00). Esto facilita que el
    modelo presente los datos de forma organizada sin necesidad de instrucciones
    de formato muy específicas en el prompt.

    Estructura de entrada (de la API):
        {
            "registros": [{"id", "entrada", "salida", "segundos_trabajados",
                           "estado", "origen", "comentario_justificacion"}, ...],
            "total_horas_trabajadas": "HH:MM:SS"
        }

    Estructura de salida (para el modelo):
        {
            "dias": [
                {
                    "fecha": "dd/mm/aaaa",
                    "registros": [{"id", "entrada", "salida", "tiempo_trabajado",
                                   "estado", "comentario"}, ...]
                }, ...
            ],
            "total_horas_trabajadas": "HH:MM:SS"
        }
    """
    from collections import defaultdict
    dias: dict[str, list] = defaultdict(list)

    for r in data.get("registros", []):
        entrada_local = _ts_local(r.get("entrada"))
        salida_local = _ts_local(r.get("salida"))
        # La fecha del día se extrae de la hora de entrada local (primeros 10 chars: dd/mm/aaaa)
        fecha = entrada_local[:10] if entrada_local else "Sin fecha"

        dias[fecha].append({
            "id": r.get("id"),
            "entrada": entrada_local,
            "salida": salida_local,
            "tiempo_trabajado": _segundos_a_hhmm(r.get("segundos_trabajados")),
            "estado": r.get("estado"),
            "comentario": r.get("comentario_justificacion"),
        })

    # Ordenar por fecha ascendente para presentar cronológicamente.
    return {
        "dias": [{"fecha": f, "registros": regs} for f, regs in sorted(dias.items())],
        "total_horas_trabajadas": data.get("total_horas_trabajadas", "00:00:00"),
    }


# ── Ejecución de herramientas ─────────────────────────────────────────────────

def _execute_tool(name: str, arguments: dict, user_token: str) -> str:
    """
    Despacha la llamada a la función solicitada por el modelo y devuelve el resultado.

    El resultado se devuelve siempre como string JSON con la estructura:
        { "success": true/false, "data": {...} }  (éxito)
        { "success": false, "error": "mensaje" }   (error)

    Esta estructura explícita es importante: el modelo debe saber claramente
    si la operación tuvo éxito o falló para no generar respuestas incorrectas
    al usuario (p.ej. decir "registrado con éxito" cuando la API devolvió 401).

    Manejo de errores HTTP
    ----------------------
    - 401: Token expirado o inválido. Indica al usuario que debe volver a login.
    - 409: Conflicto de estado (entrada pendiente, solapamiento). Se devuelve
           el mensaje 'detail' de la API para informar al usuario del motivo.
    - Otros: Error genérico con el código HTTP.
    """
    try:
        if name == "fichar_entrada":
            result = api_client.fichar_entrada(user_token)
        elif name == "fichar_salida":
            result = api_client.fichar_salida(user_token)
        elif name == "corregir_entrada":
            result = api_client.corregir_entrada(
                user_token,
                arguments["momento_entrada"],
                arguments["comentario_justificacion"],
            )
        elif name == "corregir_salida":
            result = api_client.corregir_salida(
                user_token,
                arguments["momento_salida"],
                arguments["comentario_justificacion"],
            )
        elif name == "consultar_horas":
            result = api_client.consultar_horas(
                user_token,
                arguments.get("fecha_desde"),
                arguments.get("fecha_hasta"),
            )
        else:
            return json.dumps({"success": False, "error": f"Función desconocida: {name}"})

        status = result.get("status", 0)

        if status in (200, 201):
            data = result["data"]
            # Transformar la respuesta de consultar_horas antes de pasarla al modelo.
            if name == "consultar_horas":
                data = _formatear_consulta(data)
            return json.dumps({"success": True, "data": data})

        elif status == 401:
            # El token del usuario ha expirado o no es válido para esta API.
            return json.dumps({
                "success": False,
                "error": "Token de usuario expirado o inválido. El usuario debe volver a iniciar sesión."
            })

        elif status == 409:
            # Conflicto de negocio: la API devuelve el motivo en 'detail'.
            return json.dumps({
                "success": False,
                "error": result["data"].get("detail", "Conflicto: operación no permitida en el estado actual.")
            })

        else:
            return json.dumps({
                "success": False,
                "error": result["data"].get("detail", f"Error HTTP {status}")
            })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ── Gestión del ciclo de conversación ─────────────────────────────────────────

def ensure_tools_configured():
    """
    Verifica la conectividad con el modelo al arrancar la aplicación.

    Se llama desde app.py en el bloque __main__ antes de iniciar el servidor
    Flask. Si falla, la app sigue arrancando (solo imprime un warning) para
    no bloquear el despliegue por problemas transitorios de red.
    """
    try:
        client = _get_client()
        client.models.list()
        print("[INFO] Conexión con grok-4.3 OK.")
    except Exception as e:
        print(f"[WARN] No se pudo verificar la conexión con el modelo: {e}")


def send_message(thread_id: str | None, user_message: str, user_token: str) -> tuple[str, str]:
    """
    Envía un mensaje al modelo y gestiona el bucle completo de function calling.

    Implementa el patrón ReAct (Reason + Act):
      1. El modelo razona sobre el mensaje del usuario.
      2. Si decide usar una herramienta, la solicita vía tool_calls.
      3. Ejecutamos la herramienta y devolvemos el resultado al modelo.
      4. El modelo razona sobre el resultado y puede solicitar más herramientas.
      5. Cuando el modelo genera texto sin tool_calls, ese es la respuesta final.

    El historial completo (system + user + assistant + tool) se mantiene en
    _threads[thread_id] para dar contexto al modelo en turnos sucesivos.

    Parámetros
    ----------
    thread_id : str | None
        ID del hilo de conversación. None crea un hilo nuevo.
    user_message : str
        Texto del mensaje del usuario.
    user_token : str
        Token JWT del usuario para autenticar las llamadas a la API REST.

    Retorna
    -------
    tuple[str, str]
        (texto_respuesta, thread_id) — el thread_id se persiste en la sesión
        Flask para mantener el contexto en el próximo mensaje.
    """
    import uuid

    # Crear un nuevo hilo o recuperar el existente.
    if not thread_id or thread_id not in _threads:
        thread_id = str(uuid.uuid4())
        _threads[thread_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    messages = _threads[thread_id]
    messages.append({"role": "user", "content": user_message})

    client = _get_client()

    # Bucle de function calling: continúa mientras el modelo solicite herramientas.
    while True:
        response = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",  # El modelo decide si usar herramientas o responder directamente.
        )

        msg = response.choices[0].message

        # Añadir la respuesta del asistente al historial (necesario para el contexto).
        messages.append(msg)

        # Si no hay tool_calls, el modelo ha generado la respuesta final.
        if not msg.tool_calls:
            return msg.content, thread_id

        # Ejecutar cada herramienta solicitada y añadir el resultado al historial.
        for tc in msg.tool_calls:
            arguments = json.loads(tc.function.arguments or "{}")
            output = _execute_tool(tc.function.name, arguments, user_token)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,   # Vincula el resultado con la tool_call del modelo.
                "content": output,
            })
        # Continuar el bucle: el modelo procesará los resultados de las herramientas.
