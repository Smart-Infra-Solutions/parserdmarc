"""Entry point: start the metrics HTTP server and run the polling loop."""

import logging
import signal
import threading

from prometheus_client import start_http_server

from . import metrics
from .config import Config
from .graph_client import GraphClient
from .processor import Processor
from .storage import Storage


def main():
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("parserdmarc")
    cfg.validate()

    storage = Storage(cfg)
    graph = GraphClient(cfg)
    processor = Processor(cfg, graph, storage)

    start_http_server(cfg.metrics_port)
    metrics.EXPORTER_UP.set(1)
    log.info(
        "Exporter on :%d/metrics — polling folder '%s' of %s every %ds (post-action: %s)",
        cfg.metrics_port,
        cfg.reports_folder,
        cfg.mailbox,
        cfg.poll_interval,
        cfg.post_process_action,
    )

    stop = threading.Event()

    def _shutdown(_signum, _frame):
        log.info("Signal received, shutting down…")
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop.is_set():
        try:
            processor.poll_once()
        except Exception as exc:  # never let the loop die
            log.exception("Unexpected error in poll loop: %s", exc)
            metrics.PROCESSING_ERRORS.labels("loop").inc()
        if cfg.run_once:
            log.info("RUN_ONCE set — exiting after a single poll")
            break
        stop.wait(cfg.poll_interval)

    metrics.EXPORTER_UP.set(0)


if __name__ == "__main__":
    main()
