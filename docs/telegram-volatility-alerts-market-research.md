# Рынок Telegram-алертов аномальной волатильности

## Вывод

Категория уже занята: сам факт Telegram-сообщения о росте цены не является
уникальным предложением. MVP имеет смысл только как объяснимый, шумоподавленный
кросс-биржевой alert с измеримой своевременностью.

Проверенные близкие альтернативы:

- [Crypto Screener](https://telegram.me/cryptoscreenerapp) заявляет покрытие
  девяти бирж; публичный [канал](https://telegram.me/s/cryptoscreenerapp)
  описывает NATR, OI, сделки и Telegram-уведомления.
- [CoinTrendz Pump Detector](https://telegram.me/cointrendz_pumpdetector)
  заявляет 24/7 наблюдение unusual activity/pumps на нескольких биржах;
  [лента](https://telegram.me/s/cointrendz_pumpdetector) показывает price,
  процент движения и объём.
- [100eyes Crypto Scanner](https://telegram.me/s/CryptoScanner100Eyes?before=7056)
  публикует автоматические RSI/divergence и abnormal-volatility alerts.
- [TradingView Crypto Screener](https://www.tradingview.com/crypto-screener/)
  и [TradingView Alerts](https://www.tradingview.com/support/solutions/43000520149-introduction-to-tradingview-alerts/)
  закрывают ручной скрининг и пользовательские условия.
- [CoinGecko price alerts](https://www.coingecko.com/learn/how-to-set-up-the-price-alert-function-on-coingecko)
  и [CoinMarketCap](https://play.google.com/store/apps/details?hl=en_US&id=com.coinmarketcap.android)
  закрывают отслеживание заранее известных пользователю активов.

Это подтверждает наличие категории среди русскоязычной аудитории, но не
доказывает готовность платить за новый сервис.

## Дифференциация v1

1. **Кросс-биржевое подтверждение.** Сообщение указывает, на каких CEX
   подтверждено движение и есть ли расхождение.
2. **Нормализация к собственной истории монеты.** Важны volume/ATR-подобный
   score, ликвидность, cooldown и false-positive budget, а не абсолютный
   процент цены.
3. **Объяснимый alert.** Пользователь видит факт измерения, время начала и
   версию метрики, а не непрозрачную «сигнальную» рекомендацию.
4. **Русский интерфейс.** Простой текст, дисклеймер и последующие тихие часы
   либо watchlist — удобство, но не самостоятельный moat.

Не заявлять low-latency преимущество, пока не измерен closed-bar-to-Telegram
SLO на production-like canary.

## Полезные направления после v1

- ранний/подтверждённый режим с явным уровнем уверенности;
- сгруппированные уведомления и daily digest для подавленных событий;
- нейтральная карточка события с источниками и метриками;
- user watchlist, тихие часы и профили чувствительности;
- новые CEX только с отдельным contract/latency review.

Не рекомендуются на раннем этапе: автоторговля, buy/sell сигналы,
copy-trading и «гарантированная» доходность.

## Операционные и правовые ограничения

- Использовать только публичные, документированные endpoints и соблюдать
  лимиты бирж. Для production data path требуются reconnect, freshness и
  наблюдаемость.
- Telegram требует rate limit, очередь, `retry_after`, повтор и
  приоритизацию. [FAQ Telegram](https://core.telegram.org/bots/faq#broadcasting-to-users)
  описывает обычный ориентир около 30 сообщений в секунду и условия
  Paid Broadcasts до 1000 сообщений в секунду.
- [Банк России](https://www.cbr.ru/press/event/?id=28213) характеризует
  криптоактивы как высокорисковые. Нужен явный информационный дисклеймер.
- Российское регулирование меняется. Перед монетизацией, рекламой или
  геотаргетингом нужен независимый актуальный legal review; этот документ
  не является юридическим заключением.
