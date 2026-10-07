# Coin logo sources

Every coin mark the web app shows comes from this folder and is served from our own domain. The table below is the single source of truth: the app parses it at build time for the `/logo-credits` page and to decide which coins get a logo. To add or swap a logo, drop the file into `frontend/public/logos/` and fill in its row here. No code change is needed.

Columns: **Coin** (symbol); **File** (name in this folder, empty = the coin shows a letter circle in its asset colour); **Shape** (`circle` fills the 32 px plate, `mark` sits at two thirds inside it); **Source**; **Source URL**; **Licence or terms**; **Notice** (text shown to users on the credits page, for example an attribution the licence requires).

Logos are decorative in the UI (`alt=""`): the symbol and name are always shown next to them. SVG preferred; PNG at 64 px (2x of the 32 px display size).

| Coin | File | Shape | Source | Source URL | Licence or terms | Notice |
|---|---|---|---|---|---|---|
| BTC | btc.svg | circle | Wikimedia Commons, the orange Bitcoin logo by bitboy (2010), as documented on the Bitcoin wiki | https://commons.wikimedia.org/wiki/File:Bitcoin.svg | Public domain | |
| ETH | eth.svg | mark | ethereum.org brand assets, ETH diamond (colour) | https://ethereum.org/en/assets/ | CC BY 4.0, Ethereum Foundation | ETH diamond by the Ethereum Foundation, licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). |
| SOL | sol.svg | mark | solana.com/branding, Solana logomark | https://solana.com/branding | Solana Foundation brand guidelines: shown to reference the asset, unaltered, no affiliation implied | |
| XRP | xrp.svg | mark | The community XRP symbol (2018), as adopted on xrpl.org | https://xrpsymbol.github.io/ | Community symbol, no licence file published; a simple geometric shape | |
| XLM | | | Letter circle. The Stellar Development Foundation's 2026 logo pack carries only the full lockup; the symbol file was requested from SDF | https://stellar.org/brand-resources | Pending | |
| USDC | usdc.svg | circle | circle.com/pressroom brand kit, USDC token logo | https://www.circle.com/pressroom | Circle Brand Use Policy: used to identify the asset, unaltered, at or above the 32 px minimum, no endorsement implied | |
| DOGE | doge.png | circle | Dogecoin Core repository, share/pixmaps/dogecoin256.png, resized to 64 px | https://github.com/dogecoin/dogecoin | MIT (repository licence); the Dogecoin Foundation welcomes community use of the logo | |
| SHIB | | | Letter circle. shib.io publishes only a wordmark, all rights reserved, no media kit | https://shib.io/ | No licence published | |
| PEPE | | | Letter circle. pepe.vip shows meme artwork and no token mark or licence | https://www.pepe.vip/ | No licence published | |
| BONK | | | Letter circle. brand.bonkcoin.com shows the master logo but publishes no licence; a licensed file was requested on their Telegram | https://brand.bonkcoin.com/ | Pending | |
