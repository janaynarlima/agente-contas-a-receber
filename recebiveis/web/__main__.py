"""Servidor de desenvolvimento: python -m recebiveis.web (somente localhost)."""

from recebiveis.web import criar_app

if __name__ == "__main__":
    criar_app().run(host="127.0.0.1", port=8000, debug=False)
