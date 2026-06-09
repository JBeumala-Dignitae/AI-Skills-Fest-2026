"""
app.py
======
Aplicación web Flask: interfaz de chat con autenticación Entra ID.

Responsabilidad
---------------
Punto de entrada de la webapp. Gestiona:
  1. Autenticación de usuarios mediante el flujo OAuth2 Authorization Code
     de Microsoft Entra ID (antiguo Azure AD) usando la librería MSAL.
  2. Almacenamiento seguro del token de acceso y el hilo de conversación
     en la sesión Flask (cookie firmada con SECRET_KEY).
  3. Enrutado de mensajes del chat al módulo agent_runner, que ejecuta
     el modelo de lenguaje y las herramientas de la API.

Flujo de autenticación
----------------------
  /login  →  Entra ID (Microsoft)  →  /callback  →  /chat

  El scope solicitado es 'api://4dd73579-.../access_as_user', que produce
  un token JWT con audience 'api://4dd73579-...'. Este es el mismo audience
  que valida la API FastAPI en auth.py.

  NOTA IMPORTANTE: El token obtenido es de formato v1 (issuer: sts.windows.net)
  aunque el scope usa el prefijo 'api://'. Esto es un comportamiento de Entra ID
  cuando accessTokenAcceptedVersion del API manifest no está fijado a 2.
  La API ya está adaptada para aceptar este formato.

Gestión de hilos de conversación
---------------------------------
Cada sesión de usuario mantiene un 'thread_id' que identifica el hilo de
conversación en memoria (dict en agent_runner._threads). Al hacer logout o
pulsar "Nueva conversación", el thread_id se pone a None y el agente inicia
un nuevo contexto sin memoria de mensajes anteriores.

Variables de entorno (fichero .env)
------------------------------------
  SECRET_KEY   - Clave para firmar las cookies de sesión Flask.
  CLIENT_SECRET- Secreto del App Registration de la webapp en Entra ID.
  REDIRECT_URI - URI de redirección OAuth2 (local o producción).
"""

import os
from flask import Flask, session, redirect, url_for, request, render_template, jsonify
import msal
from dotenv import load_dotenv
import agent_runner

# Carga las variables de entorno desde el fichero .env (solo en local;
# en Azure App Service se configuran como App Settings).
load_dotenv()

app = Flask(__name__)

# SECRET_KEY firma las cookies de sesión. En producción debe ser un valor
# largo y aleatorio, nunca hardcodeado en el código.
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-this")

# ── Configuración de Entra ID ─────────────────────────────────────────────────

# Tenant ID del directorio de Microsoft Entra ID donde están registradas las apps.
TENANT_ID = "c1f7b851-****-****-****-************"

# Client ID del App Registration de ESTA webapp (cliente-registro-horario).
# Diferente del CLIENT_ID de la API; cada app tiene su propio registro.
CLIENT_ID = "c7f7e1c3-****-****-****-************"

# Secreto del App Registration de la webapp. Se usa para el flujo confidencial
# (Authorization Code con client_secret), necesario en aplicaciones servidor.
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# Client ID del App Registration de la API FastAPI. Se usa para construir
# el scope del token que necesita la API para validar al llamante.
API_CLIENT_ID = "4dd73579-****-****-****-************"

# URI de redirección tras el login. Debe estar registrada en el App Registration
# de Entra ID. En local: http://localhost:5000/callback
# En Azure: https://ws-dignitae-webapp.azurewebsites.net/callback
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")

# Endpoint de autorización del tenant.
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# Scope solicitado: permiso delegado 'access_as_user' de la API.
# El token resultante permite llamar a la API en nombre del usuario.
SCOPES = [f"api://{API_CLIENT_ID}/access_as_user"]


# ── Helpers de autenticación ──────────────────────────────────────────────────

def _build_msal_app():
    """
    Crea una instancia de ConfidentialClientApplication (MSAL).

    Se usa ConfidentialClient (en lugar de PublicClient) porque la webapp
    corre en servidor y puede guardar el CLIENT_SECRET de forma segura.
    """
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


def _get_token_from_cache():
    """
    Intenta obtener un token válido desde la caché MSAL sin interacción del usuario.

    Útil para renovar silenciosamente tokens expirados si hay un refresh_token
    disponible. No se usa activamente en el flujo principal actual, pero está
    disponible para futuras mejoras de renovación automática.
    """
    msal_app = _build_msal_app()
    accounts = msal_app.get_accounts()
    if accounts:
        result = msal_app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    return None


def login_required(f):
    """
    Decorador que protege rutas: redirige a /login si el usuario no está autenticado.

    Comprueba la presencia de 'user' en la sesión Flask. La clave 'user' se
    almacena en /callback tras una autenticación exitosa.
    """
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Rutas de autenticación ────────────────────────────────────────────────────

@app.route("/login")
def login():
    """
    Inicia el flujo OAuth2 Authorization Code.

    Genera la URL de autorización de Entra ID con el scope requerido y
    redirige el navegador del usuario a esa URL. El parámetro 'state'
    (16 bytes aleatorios) protege contra ataques CSRF.
    """
    msal_app = _build_msal_app()
    auth_url = msal_app.get_authorization_request_url(
        SCOPES,
        redirect_uri=REDIRECT_URI,
        state=os.urandom(16).hex(),
    )
    return redirect(auth_url)


@app.route("/callback")
def callback():
    """
    Recibe el código de autorización de Entra ID y lo intercambia por tokens.

    Entra ID redirige aquí tras la autenticación exitosa del usuario, incluyendo
    un 'code' de un solo uso. MSAL lo intercambia por:
      - access_token: usado para llamar a la API FastAPI en nombre del usuario.
      - id_token: contiene los claims del usuario (nombre, email, etc.).

    Los datos relevantes se guardan en la sesión Flask:
      - session["user"]: claims del id_token (nombre para mostrar en la UI).
      - session["access_token"]: token para las llamadas a la API.
      - session["thread_id"]: hilo de conversación, inicia en None.
    """
    code = request.args.get("code")
    if not code:
        return "Error en la autenticación", 400

    msal_app = _build_msal_app()
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    if "access_token" not in result:
        return f"Error al obtener token: {result.get('error_description')}", 400

    session["user"] = result.get("id_token_claims", {})
    session["access_token"] = result["access_token"]
    session["thread_id"] = None  # Nuevo hilo por cada sesión de login
    return redirect(url_for("chat"))


@app.route("/logout")
def logout():
    """
    Cierra la sesión local y redirige al endpoint de logout de Entra ID.

    Primero limpia la sesión Flask (borra la cookie), luego redirige a
    Entra ID para invalidar también el Single Sign-On del navegador.
    Sin este segundo paso, el usuario podría volver a entrar sin credenciales
    usando la sesión SSO activa de Microsoft.
    """
    session.clear()
    return redirect(
        f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('chat', _external=True)}"
    )


# ── Rutas de la aplicación ────────────────────────────────────────────────────

@app.route("/")
def index():
    """
    Ruta raíz: redirige al chat si hay sesión activa, o al login si no la hay.
    """
    if "user" in session:
        return redirect(url_for("chat"))
    return redirect(url_for("login"))


@app.route("/chat")
@login_required
def chat():
    """
    Renderiza la interfaz de chat.

    Pasa el nombre del usuario al template para mostrarlo en la cabecera.
    El nombre se extrae del claim 'name' del id_token; si no existe, se usa
    'preferred_username' (normalmente el email) como fallback.
    """
    user_name = session["user"].get("name", session["user"].get("preferred_username", "Usuario"))
    return render_template("chat.html", user_name=user_name)


@app.route("/api/message", methods=["POST"])
@login_required
def message():
    """
    Endpoint AJAX: recibe un mensaje del chat y devuelve la respuesta del agente.

    El frontend envía { "message": "<texto>" } en JSON. Este endpoint:
      1. Lee el token de acceso de la sesión (para pasarlo a la API).
      2. Lee o crea el thread_id de la conversación.
      3. Llama a agent_runner.send_message(), que gestiona el modelo LLM
         y ejecuta las herramientas (fichar, consultar, etc.).
      4. Guarda el thread_id actualizado en la sesión para mantener el contexto.
      5. Devuelve { "response": "<texto respuesta>" } al frontend.

    El token se pasa al agente porque las llamadas a la API REST deben
    realizarse en nombre del usuario autenticado (no del servidor).
    """
    data = request.get_json()
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Mensaje vacío"}), 400

    user_token = session.get("access_token")
    thread_id = session.get("thread_id")

    try:
        response, thread_id = agent_runner.send_message(thread_id, user_message, user_token)
        session["thread_id"] = thread_id
        return jsonify({"response": response})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
@login_required
def reset():
    """
    Reinicia el hilo de conversación del usuario.

    Pone thread_id a None en la sesión. La próxima llamada a send_message
    creará un nuevo hilo vacío, perdiendo el contexto de la conversación
    anterior. Útil para empezar desde cero sin hacer logout.
    """
    session["thread_id"] = None
    return jsonify({"ok": True})


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Al arrancar, verifica que la conexión con el modelo LLM funciona.
    agent_runner.ensure_tools_configured()
    # debug=False en producción para no exponer trazas de error al cliente.
    app.run(host="0.0.0.0", port=5000, debug=False)
