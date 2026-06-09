"""
Obtiene un token de Entra ID abriendo el navegador.
Uso: python get_token.py
Requiere redirect URI http://localhost:8080/callback en el App Registration.
"""
import urllib.parse
import urllib.request
import webbrowser
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

TENANT_ID = "c1f7b851-****-****-****-************"
# Usar el App Registration de la WEBAPP (cliente OAuth2), no el de la API.
# La webapp es quien solicita tokens en nombre del usuario.
CLIENT_ID = "c7f7e1c3-****-****-****-************"
CLIENT_SECRET = "vLW8Q************************************"
REDIRECT_URI = "http://localhost:8080/callback"
# El scope debe referenciar el App Registration de la API con el prefijo api://
# Esto produce un token con audience = "api://4dd73579-..." que la API acepta.
API_CLIENT_ID = "4dd73579-****-****-****-************"
SCOPE = f"api://{API_CLIENT_ID}/access_as_user openid profile"

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        error = params.get("error_description", [None])[0]

        if auth_code:
            body = b"<h2>Autenticacion correcta. Puedes cerrar esta ventana.</h2>"
        else:
            body = f"<h2>Error: {error}</h2>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silencia los logs del servidor


def get_token():
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "response_mode": "query",
    })
    auth_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?{params}"

    print("Abriendo el navegador para autenticarte...")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()  # espera solo una petición

    if not auth_code:
        print("No se recibió el código de autorización.")
        return

    # Intercambiar código por token
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }).encode()

    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = json.loads(e.read().decode())
        print(f"\nError {e.code}: {e.reason}")
        print(f"  error:             {error_body.get('error')}")
        print(f"  error_description: {error_body.get('error_description')}")
        return

    token = result.get("access_token")
    if token:
        print("\n--- ACCESS TOKEN ---")
        print(token)
        print("\n--- Para usar en el header ---")
        print(f"Authorization: Bearer {token}")
    else:
        print("Error:", result)


if __name__ == "__main__":
    get_token()
