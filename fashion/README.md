# Fashion stylist

Turns Hermes into a personal stylist in its own Discord channel. Ask for an outfit ("something for a pub night, under €150") and it posts:

- an inspiration photo of the full look,
- a clean store photo of each piece, with the store, price and link. Save a photo and use it in Vinted's photo search to find the piece second-hand.

It learns your style from a short quiz the first time you talk to it, then from what you say about each look ("L12 love it, boots too chunky"). Every two weeks it posts a drop of three new looks, based on your style and current trends.

It has two parts:

| File | What it is |
|---|---|
| [`fashion.py`](fashion.py) | MCP server (standard library only). Tools: `get_profile`, `update_profile`, `image_search`, `shopping_search`, `shopping_stores`, `lens_search`, `product_info`, `save_images`, `save_look`, `rate_look`, `recent_looks` |
| [`skills/fashion-stylist/SKILL.md`](skills/fashion-stylist/SKILL.md) | Hermes skill: the quiz, how to build a look, the reply format, the fortnightly drop, and the stores to use |

By default it only uses free tools: Hermes's own `web_search` / `web_extract` / `vision_analyze`, plus a free DuckDuckGo image search in the plugin. Add a SerpApi key and it also uses Google Shopping and Google Lens (see [Paid search](#paid-search-optional)). Prices are in euro, from stores that ship to Ireland.

## Install (Hermes in Docker)

Run these from the repo folder on the server. See the [main README](../README.md) for the shared setup.

1. Copy the plugin and the skill so that root owns them:

   ```bash
   sudo mkdir -p ~/.hermes/mcp ~/.hermes/skills/fashion/fashion-stylist
   sudo cp fashion/fashion.py ~/.hermes/mcp/
   sudo chmod 644 ~/.hermes/mcp/fashion.py
   sudo cp fashion/skills/fashion-stylist/SKILL.md ~/.hermes/skills/fashion/fashion-stylist/
   ```

2. Make a `#fits` channel (you can ask Hermes to, with the Discord admin plugin) and copy its ID: in Discord, turn on Developer Mode, then right-click the channel → Copy Channel ID.

3. Add the `fashion` server and the `discord:` block from [`config.example.yaml`](../config.example.yaml) to `~/.hermes/config.yaml`, with your channel ID. This lets Hermes reply in `#fits` without an @mention and loads the stylist skill there automatically.

4. Restart Hermes and check the server connects:

   ```bash
   sudo docker restart hermes
   sudo docker exec -it -u hermes hermes hermes mcp test fashion
   ```

5. Set up the fortnightly drop. The 14 days count from when you run this, so run it at the time of day you want the drop:

   ```bash
   sudo docker exec -it -u hermes hermes hermes cron create "every 14d" "Post this fortnight's style drop." --skill fashion-stylist --deliver discord:123456789012345678
   ```

6. Say hi in `#fits`. It starts with the style quiz.

## Paid search (optional)

With a [SerpApi](https://serpapi.com) key, the stylist gets three more tools:

- `shopping_search`: Google Shopping results for Ireland, with real euro prices and a `max_price` filter, so budgets hold. Without a key it falls back to the free image search, which has no prices.
- `lens_search`: Google Lens. Given the inspiration photo and a piece ("brown loafers"), it finds matching products with full-size photos, so the store photos you take to Vinted match the look. It also works on a photo you send in `#fits` ("find me this").
- `shopping_stores`: direct store links and prices for a Google Shopping product. It's only used when the store's own page can't be found for free.

Each call uses one SerpApi search. SerpApi's free plan gives 250 searches a month, and a look takes about 6, so it should cost nothing at normal use. The plugin counts its searches and stops using SerpApi after 250 a month, going back to the free tools until the next month. If you move to a paid plan, raise the limit with `FASHION_SERPAPI_MONTHLY_LIMIT` (e.g. `1000` for the $25 plan).

To turn it on:

1. Sign up at [serpapi.com](https://serpapi.com) (free plan, no card) and copy your API key.
2. Add it to Hermes's `.env`, then restart Hermes:

   ```bash
   printf '\nSERPAPI_KEY=your-key-here\n' | sudo tee -a ~/.hermes/.env > /dev/null
   sudo docker restart hermes
   ```

Photos you send for Lens are uploaded to SerpApi, which accepts at most 500 KB. Bigger photos are shrunk automatically if Pillow is installed in the container; otherwise Lens asks for the photo's web link instead. Only photos in Hermes's cache folders can be uploaded.

## Where things are kept

- Style profile and past looks: `~/.hermes/fashion/` (`profile.json`, `looks.json`), plus `serpapi_usage.json` for this month's SerpApi count.
- Saved photos: `~/.hermes/cache/images/fashion/`. Hermes can only post files from its cache folders. Photos older than 60 days are deleted when the plugin starts; set `FASHION_KEEP_IMAGE_DAYS` to change that.

## Known limits

- **Image search can pause.** DuckDuckGo shows a bot check if it gets too many searches (in testing it lasted under an hour). The plugin spaces searches out and stops as soon as it sees a check. Until then, the skill finds products with Hermes's `web_search`, or with Google Shopping and Lens if paid search is on.
- **Some stores block reading.** ASOS blocks photo downloads, and ASOS, Zalando, H&M and Mango (and probably others) block `product_info`. The skill then reads prices with `web_extract`, and only suggests ASOS pieces if it can show a photo from elsewhere. Zalando, Zara, Uniqlo, Pinterest and Shopify stores (like END.) photos save fine, and `product_info` reads every photo and price from Shopify stores.
- It suggests new pieces only; it doesn't know what's in your wardrobe.

## Test without Hermes

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"product_info","arguments":{"url":"https://www.endclothing.com/ie/products/asics-gel-nyc-sneaker-1203a739-100"}}}' \
  | HERMES_HOME=/tmp/fashion-test python3 fashion/fashion.py
```
