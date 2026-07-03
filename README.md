# lower_my_sell_price

Ad hoc script to lower every active fixed-price eBay listing by a percentage.

## Setup

No Python packages are required. The script reads `.env` automatically; do not commit your real `.env`.

1. Copy the template:

```sh
cp .env.example .env
```

2. Create/sign in to an eBay Developer account:
   [developer.ebay.com/signin](https://developer.ebay.com/signin)

3. Open Application Keys:
   [developer.ebay.com/my/keys](https://developer.ebay.com/my/keys)

4. Create a keyset for the environment you want:
   - Production: real eBay listings.
   - Sandbox: test listings only.

5. Copy the Production or Sandbox `Client ID` and `Client Secret` into `.env`:

```dotenv
EBAY_CLIENT_ID=...
EBAY_CLIENT_SECRET=...
```

6. Get a User token, not an Application token:
   - On Application Keys, click `User Tokens` next to the same keyset.
   - In `Get a User Token Here`, choose `OAuth (new security)`.
   - Sign in as the seller account that owns the listings and agree.
   - For quick ad hoc use, paste the returned token into `.env`. Leave `EBAY_REFRESH_TOKEN` blank.

```dotenv
EBAY_OAUTH_ACCESS_TOKEN=...
```

7. For repeat use, use a refresh token instead:
   - In `User Tokens`, create/configure the RuName if prompted.
   - Follow eBay's authorization-code flow to exchange the returned `code` for a token response.
   - Production token endpoint: `https://api.ebay.com/identity/v1/oauth2/token`
   - Sandbox token endpoint: `https://api.sandbox.ebay.com/identity/v1/oauth2/token`

```sh
curl -X POST "https://api.ebay.com/identity/v1/oauth2/token" \
  -u "CLIENT_ID:CLIENT_SECRET" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=AUTH_CODE_FROM_REDIRECT" \
  --data-urlencode "redirect_uri=RUNAME"
```

   The JSON response includes `refresh_token`. Copy it into `.env` and leave `EBAY_OAUTH_ACCESS_TOKEN` blank.

```dotenv
EBAY_REFRESH_TOKEN=...
```

8. Set the marketplace and environment in `.env`:

```dotenv
EBAY_SITE_ID=0
EBAY_ENV=production
```

Common `EBAY_SITE_ID` values: `0` US, `15` Australia, `3` UK.

Helpful eBay docs:
- [Authorization guide](https://developer.ebay.com/develop/guides-v2/authorization)
- [GetMyeBaySelling](https://developer.ebay.com/devzone/xml/docs/reference/ebay/GetMyeBaySelling.html)
- [ReviseInventoryStatus](https://developer.ebay.com/devzone/xml/docs/reference/ebay/ReviseInventoryStatus.html)

## Run

Dry run to stdout:

```sh
python3.14 lower_ebay_prices.py --percent 10
```

CSV to stdout:

```sh
python3.14 lower_ebay_prices.py --percent 10 --csv
```

CSV to a file:

```sh
python3.14 lower_ebay_prices.py --percent 10 --csv --output price_changes.csv
```

Apply live changes:

```sh
python3.14 lower_ebay_prices.py --percent 10 --apply
```

Output columns: `Date/Time`, `Listing name`, `URL`, `Previous price`, `New price`, `Delta`.

Listings created through eBay's Inventory API may fail; eBay requires those to be revised through the Inventory API instead.
