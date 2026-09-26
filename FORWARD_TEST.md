# Live forward-test setup

This package adds a separate live-forward control plane for the existing BTC/IRT and USDT/IRT profiles. It does not alter the Demo worker commands. The admin site is bound to `127.0.0.1:18653`; the read-only chart dashboard is bound to `127.0.0.1:18652`.

## Install beside the existing service

Install this project at `/opt/market-maker-forward` without replacing the existing project or its service files. Use a dedicated Linux account for the worker agent and a different account for the web UI. Install `requirements.txt` into `/opt/market-maker-forward/venv` with Python 3.10 or newer. Copy the three `deploy/market-maker-forward-*.service` files into `/etc/systemd/system/`.

Create a shared group called `marketmaker-control`; add `marketmaker-admin` and `marketmaker-forward` to it. Create `/var/lib/market-maker-forward/{control,config,locks,results}` and `/var/log/market-maker-forward`. Make the state directories group-writable with setgid so the admin can enqueue commands and read config/results while only the forward agent receives the trading environment. The admin account must not be able to read `/etc/market-maker-forward.env` or the forward-agent process environment.

Create `/etc/market-maker-forward-admin.env` from `deploy/admin-forward.env.example`. Set a unique login and session secret. Generate the password hash in the installed virtual environment:

```sh
/opt/market-maker-forward/venv/bin/python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('choose-a-unique-password', method='pbkdf2:sha256'))"
```

Create `/etc/market-maker-forward.env` from `deploy/forward.env.example`; place the worker's required Nobitex environment values there. Restrict the file to root and the forward service administrator (`chmod 600`). The web service does not load `.env` files. `FORWARD_TRADING_ENABLED=1` enables the live Start controls; leave it unset or set to `0` to keep them disabled. The isolated forward runtime does not connect to MySQL; it writes its round results to its dedicated CSV result directory.

Enable the services after checking paths and ownership:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now market-maker-forward-agent market-maker-forward-admin market-maker-forward-dashboard
```

At the beginning of every real worker run, the engine reads the selected base-asset wallet (`btc` or `usdt`) and quote wallet (`rls`) through Nobitex's authenticated balance API and uses those values as the session's initial inventory and cash. These values are not editable in Live Config. If either request fails or returns an invalid value, the worker exits before sending orders. The API key must include `READ` access. Manual edits, external fills, and other bots after startup are not folded into that session's ledger; restart the worker to take a fresh starting snapshot.

The Live Config page also requests a fresh, read-only snapshot of the RLS, BTC, and USDT wallets through the credentialed forward agent whenever the page is opened or refreshed. It displays the read time. The admin web service does not receive API credentials. If the forward agent lacks credentials or a wallet request fails, the page reports that balances are unavailable instead of showing zero. Inv Min and Inv Max remain configurable strategy limits, separate from current holdings.

Open the panel through an SSH tunnel from your workstation:

```sh
ssh -L 18653:127.0.0.1:18653 -L 18652:127.0.0.1:18652 USER@SERVER
```

Then use `http://127.0.0.1:18653/admin/login` for control and `http://127.0.0.1:18652` for charts. No new public firewall port is required.

## Forward-test flow

1. Sign in to the admin panel and open **Live Config BTC** or **Live Config USDT**.
2. Review the symbol, order size, inventory bounds, quote cadence, fee assumptions, and return threshold. Both exposure guards must remain enabled; each ratio is limited to 25% in the live config validator.
3. Save the config. If a worker is already running, stop and start it to apply the new values.
4. Open **Live Forward Test**, choose one pair, and type the exact phrase shown (for example `START BTCIRT LIVE`). This starts only that profile with `algo1`, Nobitex, `--mode real`, and continuous operation.
5. Use Stop to send SIGTERM. The engine's shutdown path attempts to cancel open orders owned by that worker before exiting. Confirm order status on Nobitex after stopping.
6. Review output at the Charts page and logs in `/var/log/market-maker-forward/`.

Only BTC/IRT and USDT/IRT are wired to this panel. The UI does not accept arbitrary symbols, algorithms, API endpoints, or credentials. A changed config is saved separately from Demo and requires a worker restart. The engine's existing order ownership checks scope shutdown cancellation to orders tracked by this worker; verify the exchange order list after a stop.

## Isolation details

The forward service uses its own state, config, logs, result files, lock files, control queue, Unix accounts, and localhost-only ports. The agent launches a fixed Python argument list and does not invoke a shell. The existing application/service is not stopped or reconfigured by these units. The web process does not receive the Nobitex credentials, and the forward worker does not connect to MySQL.

This archive is source plus unit templates; it cannot verify available server ports, installed service names, disk/memory headroom, database reachability, or that the existing project is using a particular path. Check those host-specific facts before enabling the forward agent. The first real start still requires an explicit authenticated panel action and typed pair confirmation.
