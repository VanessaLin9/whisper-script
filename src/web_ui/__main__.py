"""Start the local Meeting Desk interface in a browser or an optional app window."""
import argparse
import sys
import threading
import webbrowser

from .server import WebServer


def main(argv=None):
    parser = argparse.ArgumentParser(description="啟動 Meeting Desk 本機介面")
    parser.add_argument("--app", action="store_true", help="以獨立視窗開啟（需要 pywebview）")
    args = parser.parse_args(argv)

    webview = None
    if args.app:
        try:
            import webview
        except ImportError:
            print("無法啟動獨立視窗：尚未安裝 pywebview。請執行 python3 -m pip install -r requirements-webview.txt，或改用 python3 -m src.web_ui 開啟瀏覽器模式。", file=sys.stderr)
            return 2

    try:
        server = WebServer()
    except OSError:
        server = WebServer(0)
    print(f"Meeting Desk 本機介面：{server.url}", flush=True)

    if args.app:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            webview.create_window("Meeting Desk", server.url, width=1240, height=800, min_size=(900, 600))
            webview.start()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
    else:
        webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
