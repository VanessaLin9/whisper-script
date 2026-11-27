"""Start the local Meeting Desk browser interface."""
import webbrowser

from .server import WebServer


def main():
    try:
        server = WebServer()
    except OSError:
        server = WebServer(0)
    print(f"Meeting Desk 本機網頁介面：{server.url}", flush=True)
    webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
