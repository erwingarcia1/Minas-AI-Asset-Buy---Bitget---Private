# Coin logo sources

Every coin mark the web app shows comes from this folder and is served from our own domain. The table below is the single source of truth: the app parses it at build time for the `/logo-credits` page and to decide which coins get a logo. To add or swap a logo, drop the file into `frontend/public/logos/` and fill in its row here. No code change is needed.

Columns: **Coin** (symbol); **File** (name in this folder, empty = the coin shows a letter circle in its asset colour); **Shape** (`circle` fills the 32 px plate, `mark` sits at two thirds inside it); **Source**; **Source URL**; **Licence or terms**; **Notice** (text shown to users on the credits page, for example an attribution the licence requires).

Logos are decorative in the UI (`alt=""`): the symbol and name are always shown next to them. SVG preferred; PNG at 64 px (2x of the 32 px display size). Files taken from Bitget's site are stored here once and resized only; they are never loaded from Bitget's servers.

To stop showing a logo (for example at a project's request), clear its **File** cell: the coin goes back to the lettered circle and leaves the credits table, and the "shown with their initial" line on the credits page names it again. No code change.

| Coin | File | Shape | Source | Source URL | Licence or terms | Notice |
|---|---|---|---|---|---|---|
| BTC | btc.svg | circle | Wikimedia Commons, the orange Bitcoin logo by bitboy (2010), as documented on the Bitcoin wiki | https://commons.wikimedia.org/wiki/File:Bitcoin.svg | Public domain | |
| ETH | eth.svg | mark | ethereum.org brand assets, ETH diamond (colour) | https://ethereum.org/en/assets/ | CC BY 4.0, Ethereum Foundation | ETH diamond by the Ethereum Foundation, licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). |
| SOL | sol.svg | mark | solana.com/branding, Solana logomark | https://solana.com/branding | Solana Foundation brand guidelines: shown to reference the asset, unaltered, no affiliation implied | |
| XRP | xrp.svg | mark | The community XRP symbol (2018), as adopted on xrpl.org | https://xrpsymbol.github.io/ | Community symbol, no licence file published; a simple geometric shape | |
| XLM | xlm.png | circle | Icon as displayed on Bitget (https://img.bgstatic.com/multiLang/coin_img/51cec93b9021a4e88470ef0ff6a4ff76.png); trademark of the Stellar Development Foundation; used unaltered only to identify the coin | https://www.bitget.com/spot/XLMUSDT | Trademark of the Stellar Development Foundation; used unaltered only to identify the coin | |
| USDC | usdc.svg | circle | circle.com/pressroom brand kit, USDC token logo | https://www.circle.com/pressroom | Circle Brand Use Policy: used to identify the asset, unaltered, at or above the 32 px minimum, no endorsement implied | |
| DOGE | doge.png | circle | Dogecoin Core repository, share/pixmaps/dogecoin256.png, resized to 64 px | https://github.com/dogecoin/dogecoin | MIT (repository licence); the Dogecoin Foundation welcomes community use of the logo | |
| SHIB | shib.png | circle | Icon as displayed on Bitget (https://img.bgstatic.com/multiLang/coin_img/1cdef695d6af24f286a4ce7d08b71ae5.png); trademark of the Shiba Inu project (Shib Doggy DAO Foundation); used unaltered only to identify the coin | https://www.bitget.com/spot/SHIBUSDT | Trademark of the Shiba Inu project (Shib Doggy DAO Foundation); used unaltered only to identify the coin | |
| PEPE | pepe.png | mark | Icon as displayed on Bitget (https://img.bgstatic.com/multiLang/coin_img/2ff7f38071e2127494c5f2001d5f4513.png); trademark of the Pepe token project (pepe.vip); used unaltered only to identify the coin | https://www.bitget.com/spot/PEPEUSDT | Trademark of the Pepe token project (pepe.vip); used unaltered only to identify the coin | |
| BONK | bonk.png | circle | Icon as displayed on Bitget (https://img.bgstatic.com/multiLang/coin_img/8d65ac6d6cd913a360dfe6f65be53dce.png); trademark of the Bonk project (BonkDAO); used unaltered only to identify the coin | https://www.bitget.com/spot/BONKUSDT | Trademark of the Bonk project (BonkDAO); used unaltered only to identify the coin | |
