A repository dedicated to arbitrage bots on crypto exchanges like okx, bybit, and hyperliquid.
In addition to the actual developments, there will be auxiliary scripts, such as: scripts for data collection, data analysis, various WebSocket connections, and ping checks.
	bybit_ws.py - an arbitrage bot with market orders on the Bybit/OKX pair, operating on the pre-market of perpetual futures.
	config_example.json - сonfig for API data on all crypto exchanges used.
	data_collector_okx_bybit.py - script for collecting spread data on the Bybit/OKX pair.
	okx_hyper.py - script for collecting spread data on the Hyper/OKX pair. Here data is collected for many coins at once, potentially this data collector will be used as a screener for finding 	opportunities.
	ping_bybit.py, ping_hyper.py, ping_okx.py - scripts to check market data latency.
