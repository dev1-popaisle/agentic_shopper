# A2A Shopping Agent with UCP

This project uses `uv` to manage its Python environment. Install `uv` and
Python 3.12 before setting up the project.

## Set up

From the project root:

```sh
uv python install 3.12
uv sync
```

The local virtual environment is created in `.venv/`.

## Run the server

```sh
.venv/bin/a2a-server
```

The server listens on `http://127.0.0.1:9999` and publishes its Agent Card at
`/.well-known/agent-card.json`. Its A2A skills discover merchant capabilities,
search UCP product catalogs, and prepare checkout sessions.

## Discover a store

In another terminal, discover the store's A2A Agent Card and UCP profile:

```sh
.venv/bin/a2a-client --discover https://shop.example
```

The agent also fetches the merchant's UCP profile from
`https://<store-host>/.well-known/ucp`. It supports UCP `2026-08-25` over REST
for catalog search and checkout-session creation. UCP capabilities are treated
as advertised until the corresponding operation succeeds.

Search a participating merchant's catalog:

```sh
.venv/bin/a2a-client --search https://shop.example --query "blue running shoes"
```

Create a checkout session for an item ID returned by catalog search:

```sh
.venv/bin/a2a-client --checkout https://shop.example \
  --item-id merchant-variant-id --quantity 1
```

The checkout operation creates a session only. It returns the merchant's
response for user review and does not submit payment or finalize an order;
UCP's base checkout flow requires the buyer to finish through a trusted UI.
Merchant-specific negotiation APIs are not implemented. Stripe checkout is
available only in the explicitly configured test-mode flow described below;
MPP for metered services and live payments are not enabled.

All merchant URLs and advertised UCP endpoints must use HTTPS on the standard
port and resolve only to public IP addresses. The client pins connections to
resolved public addresses, does not follow redirects, and limits response size
and request time. If configured, `UCP_AGENT_PROFILE_URL` is sent in the
`UCP-Agent` request header; set it to the public URL of this agent's UCP profile
when the merchant requires caller profile discovery. Merchant authentication
and request-signature schemes are not yet configured.

The A2A discovery report still checks the standard
`/.well-known/agent-card.json`; advertised A2A interfaces are not contacted.

## Run the local mock merchant

The test merchant uses a local DuckDB database at
`data/mock_store.duckdb`. It seeds ten real, packaged grocery products with
verified 12-digit UPC-A codes. Product photos are downloaded into
`data/product_images/` and stored locally; each database record includes the
image path, source URL, product record URL, license, and attribution. Product
data and photos come from [Open Food Facts](https://world.openfoodfacts.org/)
under CC BY-SA 3.0. Open Food Facts notes that some image contents may have
additional rights, so this catalog is intended as a local demonstration.
Displayed prices and inventory are illustrative, not live retailer offers.

On startup the merchant creates or migrates the DuckDB schema and replaces the
previous fictional seed catalog once with the real product catalog. The
storefront, JSON API, A2A search, and UCP search all query the same database.

Start the merchant in one terminal:

```sh
.venv/bin/mock-store
```

The storefront is at `http://127.0.0.1:5555`. It publishes:

- A2A Agent Card at `http://127.0.0.1:5555/.well-known/agent-card.json`
- UCP profile at `http://127.0.0.1:5555/.well-known/ucp`
- UCP search at `POST http://127.0.0.1:5555/ucp/catalog/search`
- Read-only product lookup at `GET http://127.0.0.1:5555/api/products`
  and `GET /api/products/{upc}`
- Locally served product photos at `GET /images/{upc}`
- UCP checkout creation and completion at
  `POST /ucp/checkout-sessions` and
  `POST /ucp/checkout-sessions/{id}/complete`

### Stripe sandbox checkout

The merchant advertises Stripe's `com.stripe.payments` UCP handler only when
both `STRIPE_SECRET_KEY` and `STRIPE_BUSINESS_PROFILE_ID` contain test-mode
credentials. The app deliberately rejects live-mode keys and does not read API
keys from files in `config/`. Keep credentials in environment variables or a
secrets manager. Never commit credentials; revoke any key that was exposed.

Set a Stripe test secret and the corresponding test business profile ID in the
merchant terminal, then enable the sandbox-only test-token helper:

```sh
printf "Stripe test secret key: "
read -s STRIPE_SECRET_KEY
printf "\n"
export STRIPE_SECRET_KEY
export STRIPE_BUSINESS_PROFILE_ID=profile_test_your_profile_id
export STRIPE_ENABLE_TEST_HELPER=1
.venv/bin/mock-store
```

The `--test-purchase` buyer action is intentionally limited to explicitly
enabled loopback stores. It creates a checkout, asks Stripe's test helper for a
`pm_card_visa` SPT limited to the exact checkout total and a short expiry, then
completes the checkout with that token. Stripe test mode does not charge a real
payment method. The helper is a sandbox simulation, not buyer consent for a
real purchase.

### Interactive buyer CLI

The `buyer-agent` CLI uses the merchant's A2A Agent Card for discovery, fetches
the current complete product list through UCP, then asks OpenRouter to match
the buyer's shopping question only to available products in that catalog.
OpenRouter returns product IDs; the CLI validates every ID against the fetched
catalog and displays product facts from the merchant's response, not model-
generated product claims. A request with no valid catalog match returns
“Product not found in this marketplace catalog” and cannot proceed to checkout.
It is a catalog-only shopping assistant, not a general chat assistant.
OpenRouter credentials are read from
`OPENROUTER_API_KEY` or the ignored `Config/OR_API_Key_1.rtf` file. The default
model is `openai/gpt-6.1-terra-pro`; override it with `OPENROUTER_MODEL`. Only the
shopping request is sent to OpenRouter, not the buyer profile, delivery address,
phone, email, checkout, or payment information. The API key is sent to
`https://openrouter.ai/api/v1/chat/completions` as the authorization credential.
The model only generates a search phrase: the buyer chooses the product,
accepts the price, and separately confirms payment.

The CLI negotiates price and delivery terms with the merchant over A2A. The
merchant's private policy is loaded from `Config/seller_policy.json`; it is not
published in the seller Agent Card or returned in quotes. The demo policy uses
10% off for products selling at least 50 units/day, 20% for 1-49/day, and 25%
for zero sales/day. The 50–100 sales/day range is also 10%. Waiting 7 or 14
days adds 10% or 15%, respectively, to the product discount (capped at 100%).
Delivery within the configured city ZIP prefix costs $10, waived for at least
three units. The per-UPC sales values in the checked-in policy are synthetic
demo inputs, not real sales data. Edit the JSON to change the mock merchant's
private policy. Negotiation takes place as A2A offers and counteroffers; the
buyer explicitly accepts the final quote and confirms payment before a Stripe
test-mode purchase is attempted. The accepted quote determines the checkout
price and delivery fee.

Published, product-specific discounts are separate from that private policy
and are configured in `Config/default_discount.json`, keyed by 12-digit UPC-A.
The CLI asks whether the buyer wants to explicitly check for a published offer.
If the selected UPC is listed, the merchant returns that exact discount as an
offer the buyer can accept. If it is not listed, no discount quote is returned.
The file currently lists a 5% Diet Coke offer for UPC-A `049000028911`. If the
buyer does not accept a published offer, they can explicitly choose to bargain
over price and delivery terms through A2A.

The buyer profile is stored in the local file `Config/buyer.json`. It contains
the configured email, phone, and delivery address. Buyer and seller profiles,
the seller's private policy, and credentials are excluded from the public
repository; configure them locally before running those flows. The seller's
SMTP credentials are not stored in a project config file; configure
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and
`SMTP_FROM_EMAIL` in the merchant server's environment. After payment, the
buyer sends the seller's `schedule_order` confirmation request over A2A with
the buyer's email and phone. The seller verifies the paid checkout, sends a
final item/price/fulfillment/appointment confirmation email via STARTTLS, and
returns the same order confirmation details to the buyer over A2A. A missing
seller SMTP configuration blocks appointment scheduling before payment. If the
SMTP service rejects the email after payment, the seller leaves the appointment
unconfirmed and returns an explicit A2A error; the confirmation operation can
be retried after the merchant's email service is corrected.

After accepting a price, the buyer enters a separate scheduling phase. Delivery
requests send the preferred local date and time to the seller over A2A. The
seller confirms an available slot or returns the nearest available counteroffer;
the buyer can accept it or propose another time. Store pickup is not negotiated:
the seller assigns its next appointment automatically, and the delivery fee is
not charged for pickup. The seller's address,
timezone, and hours are in `Config/seller.json` (default hours Monday-Saturday,
9:00 AM-5:00 PM, hourly, America/Los_Angeles). The seller confirms the accepted
appointment over A2A only after sandbox payment succeeds. The seller sends the
final confirmation email and returns the order details to the buyer over A2A.
An accepted delivery slot is held for 30 minutes while checkout is completed;
canceling checkout releases the hold. Pickup is assigned and held automatically.
See the [sample four-step A2A purchase walkthrough](./SAMPLE_A2A_PURCHASE_WALKTHROUGH.md)
for a Coca-Cola 12-pack example, including the limits imposed by the current
seller policy.

Start the merchant with Stripe **test-mode** values and its own SMTP settings in
one terminal:

```sh
printf "Stripe test secret key: "
read -s STRIPE_SECRET_KEY
printf "\n"
export STRIPE_SECRET_KEY
export STRIPE_BUSINESS_PROFILE_ID=profile_test_your_profile_id
export STRIPE_ENABLE_TEST_HELPER=1
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USERNAME=your-smtp-user
read -s SMTP_PASSWORD
export SMTP_PASSWORD
export SMTP_FROM_EMAIL=orders@example.com
.venv/bin/mock-store
```

In another terminal, run the buyer agent:

```sh
export A2A_ALLOW_LOCAL_TEST_STORE=1
.venv/bin/buyer-agent
```

The CLI searches products, completes the optional discount or explicit price
negotiation phase, asks the seller to schedule delivery or assign pickup, then
shows the buyer profile/order summary and requires separate explicit payment
confirmation before charging the Stripe **test** account. After payment, the
mock merchant records the appointment/address locally, sends the final
confirmation email using the SMTP environment configured on the merchant
process, and returns the same confirmed order details over A2A. The schedule
does not book a carrier or reserve real pickup inventory; this demo does not
fulfill orders.
Payment and schedule records are in-memory and are lost when the merchant
restarts. Use only a separate Stripe test secret and its matching test business
profile. Live-mode keys are neither read nor accepted.

Run the buyer in another terminal:

```sh
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-server
```

Then trigger a sandbox purchase (the UPC is also the UCP item ID):

```sh
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-client \
  --test-purchase http://127.0.0.1:5555 --item-id 049000028911
```

A production buyer flow would obtain an SPT through buyer-side authorization
(for example, Stripe Link Agent Wallet), and the merchant would accept only
that scoped token rather than raw card details. This project does not implement
buyer wallet authorization, production Stripe keys, AP2 Mandates, or live
checkout. UCP's base checkout flow requires a trusted buyer UI unless a
supported authorization extension is in place; do not use the test helper or
seller API key to simulate real buyer authorization.

In another terminal, opt the buyer agent into loopback-only HTTP testing and
start it:

```sh
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-server
```

Then exercise merchant discovery, product search, and checkout-session creation:

```sh
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-client --discover http://127.0.0.1:5555
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-client \
  --search http://127.0.0.1:5555 --query "Peanut Butter"
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/a2a-client \
  --checkout http://127.0.0.1:5555 --item-id 049000028911
```

Replace the example item ID with a variant ID returned by search; in this mock
catalog the variant ID is the same as its UPC. The local-test setting accepts
only literal loopback IPs and requires an explicit port. Without it, the buyer
agent continues to require HTTPS and public destinations. Checkout creation
alone does not charge a payment method; use the explicit sandbox purchase flow
above to test Stripe completion.

The mock merchant also supports direct A2A catalog search:

```sh
.venv/bin/a2a-client --url http://127.0.0.1:5555 '{"query":"cereal"}'
```
