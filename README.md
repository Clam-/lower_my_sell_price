# lower_my_sell_price

Ad hoc script to lower every active fixed-price eBay listing by a percentage.

## Setup

1. Create an eBay Developer app and Production keyset.
2. Generate a User OAuth token, not an Application token. For repeat use, get a refresh token with selling/listing scopes; `sell.inventory` is normally needed.
3. Export credentials:

```sh
export EBAY_CLIENT_ID="..."
export EBAY_CLIENT_SECRET="..."
export EBAY_REFRESH_TOKEN="..."
```

Or use a short-lived access token:

```sh
export EBAY_OAUTH_ACCESS_TOKEN="..."
```

Optional:

```sh
export EBAY_SITE_ID="0"      # 0=US, 15=Australia, 3=UK
export EBAY_ENV="sandbox"    # only for sandbox testing
```

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
