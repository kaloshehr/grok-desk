"""Single process. Default: winners + gems + Telegram + publisher (alerts-only).
Old Scanner/Auditor/Analyst/Executor/Risk/etc. stay in the repo and start only when
desk.legacy_pipeline is true."""
import asyncio, time, threading, signal, db, config, notifier, publisher, util
from agents import winners, gems
log = util.logger("main")

LEGACY_LOOPS = ("scanner.poll_loop", "prefilter.loop", "trend.loop", "wallets.loop", "copytrade.loop",
                "learning.loop", "auditor.loop", "analyst.loop", "executor.loop", "risk.loop", "momentum.loop")

def _legacy_loops():
    from agents import prefilter, trend, scanner, auditor, analyst, executor, risk, wallets, copytrade, momentum
    import learning
    return (scanner.poll_loop, prefilter.loop, trend.loop, wallets.loop, copytrade.loop, learning.loop,
            auditor.loop, analyst.loop, executor.loop, risk.loop, momentum.loop)

def _desk_beat(stop):
    while not stop.is_set():
        db.beat("desk"); stop.wait(15)

def main():
    s = config.load(); db.q("select 1")
    if db.kv_get("paper_balance") is None: db.kv_set("paper_balance", s["paper_balance"])
    if s["mode"] == "PAPER" and not db.kv_get("paper_since"): db.kv_set("paper_since", time.time())
    legacy = bool(s.get("legacy_pipeline"))
    if legacy:
        from agents.executor import ticket_check
        ticket_check(push=True)
    stop = threading.Event()
    loops = (( _legacy_loops() if legacy else () ) + (winners.loop, gems.loop, notifier.loop, publisher.loop, _desk_beat))
    for f in loops:
        threading.Thread(target=f, args=(stop,), daemon=True, name=getattr(f, "__module__", None) or f.__name__).start()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    log.info(f"desk up mode={s['mode']} legacy_pipeline={legacy} analyst={s['analyst_mode']}")
    if legacy:
        from agents import scanner
        asyncio.run(scanner.ws_loop(stop))
    else:
        asyncio.run(winners.ws_loop(stop))

if __name__ == "__main__": main()
