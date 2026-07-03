# lower_my_sell_price

Ad hoc scripts to authenticate with eBay and lower every active fixed-price eBay listing by a percentage.

## Setup

No Python packages are required. The Let's Encrypt helper uses the local `openssl` command. The scripts read `.env` automatically; do not commit your real `.env`.

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

6. Prepare a public HTTPS callback domain.

   You need a domain that points to the machine running these scripts. During certificate issuance, forward external port `80` to local port `8081`. During eBay login, forward external port `443` to local port `8765`.

```dotenv
LETSENCRYPT_DOMAIN=login.example.com
LETSENCRYPT_EMAIL=you@example.com
LETSENCRYPT_AGREE_TOS=true
LETSENCRYPT_STAGING=true
```

7. Test the Let's Encrypt HTTP-01 flow against staging:

```sh
python3.14 letsencrypt_handler.py
```

   If staging succeeds, request a trusted production certificate:

```sh
python3.14 letsencrypt_handler.py --production
```

   The certificate material is written under `certs/<domain>/`:
   - `fullchain.pem`
   - `privkey.pem`
   - `cert.pem`
   - `chain.pem`

8. Configure the eBay OAuth redirect URL and copy the RuName:
   - On Application Keys, click `User Tokens` next to the same keyset.
   - In `Get a Token from eBay via your Application`, create/configure Redirect URL settings if prompted.
   - Set `Auth Accepted URL` to `https://<LETSENCRYPT_DOMAIN>/callback`.
   - For `Auth Declined URL`, use any HTTPS page you control.
   - Copy the generated eBay Redirect URL name, also called the `RuName`, into `.env`.

```dotenv
EBAY_RUNAME=...
EBAY_OAUTH_ACCEPTED_URL=https://login.example.com/callback
```

   eBay's OAuth request uses the `RuName` as the `redirect_uri`; the HTTPS URL is configured inside that RuName in the Developer Portal.

9. Get and store a refresh token.

   Make sure external port `443` is forwarding to local port `8765`, then run:

```sh
python3.14 ebay-login.py
```

   The login helper starts a temporary HTTPS server on `8765`, prints the eBay sign-in URL, waits for eBay's redirect to `/callback`, exchanges the authorization code, stores `EBAY_REFRESH_TOKEN` in `.env`, and clears `EBAY_OAUTH_ACCESS_TOKEN`.

   If your callback uses a different path or local port:

```sh
python3.14 ebay-login.py --port 9000 --callback-path /ebay/callback
```

   If a proxy or tunnel terminates TLS and forwards plain HTTP to this script:

```sh
python3.14 ebay-login.py --http
```

   eBay Trading API OAuth calls do not use scopes. The helper sends eBay's base OAuth scope by default; if your Developer Portal sample requires a specific scope list, set `EBAY_OAUTH_SCOPES` in `.env` or pass `--scopes`.

10. Optional: for quick ad hoc use, paste a short-lived User access token into `.env` and leave `EBAY_REFRESH_TOKEN` blank:

```dotenv
EBAY_OAUTH_ACCESS_TOKEN=...
```

11. Set the marketplace and environment in `.env`:

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
