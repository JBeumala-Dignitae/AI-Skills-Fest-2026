"""
api_client.py
=============
Cliente HTTP para la API REST de registro horario.

Responsabilidad
---------------
Encapsula todas las llamadas a la API FastAPI desplegada en Azure App Service
(ws-dignitae.azurewebsites.net). Cada función recibe el token JWT del usuario
autenticado (obtenido por MSAL en app.py) y lo incluye en la cabecera
Authorization: Bearer <token>.

Por qué este módulo existe
--------------------------
Separar las llamadas HTTP del resto de la lógica permite:
  - Cambiar fácilmente entre entorno local y producción (API_BASE).
  - Centralizar el manejo de cabeceras y serialización.
  - Facilitar los tests unitarios mockeando este módulo.

Formato de respuesta
--------------------
Todas las funciones devuelven un dict con la forma:
    {"status": <código HTTP>, "data": <cuerpo JSON de la respuesta>}
El código HTTP se usa en agent_runner.py para distinguir éxito (200/201)
de errores conocidos (401 token expirado, 409 conflicto, etc.).
"""

import requests

# URL base de la API REST.
# LOCAL:      "http://localhost:8001"
# PRODUCCIÓN: "https://ws-dignitae.azurewebsites.net"
API_BASE = "https://ws-dignitae.azurewebsites.net"


def _headers(token: str) -> dict:
    """
    Construye las cabeceras HTTP comunes para todas las peticiones.

    El token JWT se obtiene del flujo OAuth2 Authorization Code (MSAL) y tiene
    como audience 'api://4dd73579-****-****-****-************', que es el
    identificador de la App Registration de la API en Entra ID.
    """
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fichar_entrada(token: str) -> dict:
    """
    Registra la entrada del empleado en el momento actual.

    La API asigna el timestamp usando la hora local del servidor (+02:00).
    El campo 'origen' se envía como 'agenteIA' para distinguir estos
    registros de los generados por una webapp directa.

    Posibles respuestas:
      201 - Entrada registrada correctamente.
      409 - Ya existe una entrada pendiente de salida (no se puede fichar
            dos entradas consecutivas sin fichar la salida).
    """
    r = requests.post(
        f"{API_BASE}/fichar-entrada",
        json={"origen": "agenteIA"},
        headers=_headers(token),
    )
    return {"status": r.status_code, "data": r.json()}


def fichar_salida(token: str) -> dict:
    """
    Registra la salida del empleado en el momento actual.

    La API busca el registro más reciente en estado 'salida pendiente'
    del empleado y lo completa con la hora actual, calculando los
    segundos trabajados.

    Posibles respuestas:
      200 - Salida registrada y registro completado.
      404 - No hay ningún registro pendiente de salida.
      409 - La jornada se solaparía con un registro ya existente.
    """
    r = requests.post(
        f"{API_BASE}/fichar-salida",
        json={"origen": "agenteIA"},
        headers=_headers(token),
    )
    return {"status": r.status_code, "data": r.json()}


def corregir_entrada(token: str, momento_entrada: str, comentario_justificacion: str) -> dict:
    """
    Registra una entrada con una hora pasada específica (corrección manual).

    El momento_entrada debe ser anterior al instante actual y no puede
    solaparse con ningún registro completo ya existente del empleado.
    El comentario es obligatorio para justificar la corrección.

    Parámetros
    ----------
    momento_entrada : str
        Fecha y hora en formato 'aaaa-mm-dd hh:mm:ss'. La API la interpreta
        como hora local (+02:00).
    comentario_justificacion : str
        Motivo de la corrección. Se almacena en el campo correspondiente
        del registro.

    Posibles respuestas:
      201 - Entrada corregida y registro creado.
      400 - El momento es futuro o formato incorrecto.
      409 - Solapamiento con registro existente.
    """
    r = requests.post(
        f"{API_BASE}/corregir-entrada",
        json={
            "momento_entrada": momento_entrada,
            "comentario_justificacion": comentario_justificacion,
        },
        headers=_headers(token),
    )
    return {"status": r.status_code, "data": r.json()}


def corregir_salida(token: str, momento_salida: str, comentario_justificacion: str) -> dict:
    """
    Registra una salida con una hora pasada específica (corrección manual).

    La API busca el registro pendiente de salida más reciente cuya entrada
    sea anterior al momento_salida indicado. Si el comentario ya existe en
    el registro, el nuevo se concatena con ' | '.

    Parámetros
    ----------
    momento_salida : str
        Fecha y hora en formato 'aaaa-mm-dd hh:mm:ss'. La API la interpreta
        como hora local (+02:00).
    comentario_justificacion : str
        Motivo de la corrección.

    Posibles respuestas:
      200 - Salida corregida y registro completado.
      404 - No hay registro pendiente anterior al momento indicado.
      409 - Solapamiento con registro existente.
    """
    r = requests.post(
        f"{API_BASE}/corregir-salida",
        json={
            "momento_salida": momento_salida,
            "comentario_justificacion": comentario_justificacion,
        },
        headers=_headers(token),
    )
    return {"status": r.status_code, "data": r.json()}


def consultar_horas(token: str, fecha_desde: str = None, fecha_hasta: str = None) -> dict:
    """
    Consulta los registros de jornada del empleado en un rango de fechas.

    Si no se proporcionan fechas, la API devuelve los registros del día actual.
    La respuesta incluye la lista de registros y el total de horas trabajadas
    en el rango (campo 'total_horas_trabajadas' en formato HH:MM:SS).

    Parámetros
    ----------
    fecha_desde : str, opcional
        Fecha de inicio en formato 'aaaa-mm-dd'.
    fecha_hasta : str, opcional
        Fecha de fin en formato 'aaaa-mm-dd'.

    Posibles respuestas:
      200 - Lista de registros (puede ser vacía si no hay datos).
    """
    params = {}
    if fecha_desde:
        params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        params["fecha_hasta"] = fecha_hasta

    r = requests.get(
        f"{API_BASE}/consultar-horas",
        params=params,
        headers=_headers(token),
    )
    return {"status": r.status_code, "data": r.json()}
