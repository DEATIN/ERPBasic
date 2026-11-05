"""Локальный запуск демонстрации без установки зависимостей."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


if __name__ == '__main__':
    directory = Path(__file__).resolve().parents[1] / 'dist'
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    with ThreadingHTTPServer(('127.0.0.1', 8000), handler) as server:
        print('ERP Basic: http://127.0.0.1:8000/ — для остановки нажмите Ctrl+C')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nДемонстрация остановлена.')
