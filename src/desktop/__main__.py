"""One JSON request on stdin; newline-delimited progress and result on stdout."""
import json
import signal
import sys

from src.common.cancellation import CancellationController, OperationCancelled
from .service import DesktopService, dispatch


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    controller = CancellationController()
    signal.signal(signal.SIGTERM, lambda *_: controller.cancel())
    signal.signal(signal.SIGINT, lambda *_: controller.cancel())
    try:
        request = json.load(sys.stdin)
        result = dispatch(DesktopService(), request, controller.token,
                          lambda event: emit({"type": "progress", **event}))
        emit({"type": "result", "ok": True, **result})
        return 0
    except Exception as exc:
        emit({"type": "result", "ok": False, "cancelled": isinstance(exc, OperationCancelled), "error": str(exc)})
        return 130 if isinstance(exc, OperationCancelled) else 1


if __name__ == "__main__":
    raise SystemExit(main())
