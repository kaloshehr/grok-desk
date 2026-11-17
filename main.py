"""Single process: all 5 agents + Telegram + publisher."""
import asyncio, time, threading, signal, sys, os, db, config, notifier, publisher, util
from agents import prefilter, trend, scanner, auditor, analyst, executor, risk, wallets, copytrade, momentum, winners, gems
import learning
log = util.logger("main")
def main():
    s = config.load(); db.q("select 1")
    if db.kv_get("paper_balance") is None: db.kv_set("paper_balance", s["paper_balance"])
    if s["mode"] == "PAPER" and not db.kv_get("paper_since"): db.kv_set("paper_since", time.time())
    from agents.executor import ticket_check
    ticket_check(push=True)  # night start
    stop = threading.Event()
    for f in (scanner.poll_loop, prefilter.loop, trend.loop, wallets.loop, copytrade.loop, learning.loop, auditor.loop, analyst.loop, executor.loop, risk.loop, momentum.loop, winners.loop, gems.loop, notifier.loop, publisher.loop):
        threading.Thread(target=f, args=(stop,), daemon=True, name=f.__module__).start()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    log.info(f"desk up mode={s['mode']} analyst={s['analyst_mode']}")
    asyncio.run(scanner.ws_loop(stop))
if __name__ == "__main__": main()
