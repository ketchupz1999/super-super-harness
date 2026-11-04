"""Manual, real-clock one-minute smoke test. No real models or credentials."""
import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path
import tempfile
import time

from super_super_harness.ops import config
from super_super_harness.adapters import register
from super_super_harness.daemon import Service
from super_super_harness.events.reminders import local_stamp
from tests.fake_harness import FakeHarness
from tests.support import free_port, write_config


async def main():
    home = Path(tempfile.mkdtemp(prefix="ssharness-reminders-live-", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
    log = home / "acceptance.log"
    logging.basicConfig(filename=log, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    write_config(home, 1, 2)
    cfg = config.load(home)
    cfg.web["port"] = free_port()
    cfg.harnesses["test-live"] = {}
    cfg.profiles = {name: replace(p, harness="test-live") for name,p in cfg.profiles.items()}
    fake = FakeHarness()
    register("test-live", fake)
    print("Isolated SSHARNESS_HOME:", home, flush=True)
    print("Log:", log, flush=True)

    async def boot():
        service = Service(cfg)
        running = asyncio.create_task(service.run())
        end = time.time()+10
        while not service.router.ready:
            if time.time()>end:
                raise RuntimeError("isolated service failed to start")
            await asyncio.sleep(.05)
        return service, running

    service, running = await boot()
    try:
        due = local_stamp(time.time()+60)
        for kind in ("notify", "run"):
            result = await service.call_tool(("router", None), "reminder_add", dict(
                name="Minute check " + kind, text="Review the project notes", kind=kind, at=due))
            logging.info("scheduled %s %s due=%s", result["id"], kind, due)
        end = time.time()+75
        while time.time()<end:
            count = service.store.one("SELECT COUNT(*) n FROM reminder_fires")["n"]
            if count == 2 and not service.store.current_turn() and not service.store.messages("queued"):
                break
            await asyncio.sleep(.2)
        assert service.store.one("SELECT COUNT(*) n FROM reminder_fires")["n"] == 2
        assert service.store.one("SELECT COUNT(*) n FROM conversation WHERE kind='reminder'")["n"] == 1
        assert service.store.one("SELECT COUNT(*) n FROM messages WHERE kind='reminder.run' AND status='done'")["n"] == 1
        assert len(fake.sessions[service.router.session_id]) == 1
        logging.info("PASS notify durable outbox and run handled once by FAKE Harness; NO REAL MODEL")
    finally:
        service.stopping.set()
        await running
    service, running = await boot()
    try:
        await asyncio.sleep(3)
        assert service.store.one("SELECT COUNT(*) n FROM reminder_fires")["n"] == 2
        assert service.store.one("SELECT COUNT(*) n FROM messages WHERE kind='reminder.run'")["n"] == 1
        assert service.store.one("SELECT COUNT(*) n FROM conversation WHERE kind='reminder'")["n"] == 1
        assert len(fake.sessions[service.router.session_id]) == 1
        logging.info("PASS restart: no repeated occurrence, outbox reminder or Harness input")
    finally:
        service.stopping.set()
        await running
    print("PASS one-minute notify/run and restart deduplication; run used FAKE Harness, not a real model", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
