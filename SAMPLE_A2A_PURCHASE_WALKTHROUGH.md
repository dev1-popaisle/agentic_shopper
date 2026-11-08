# Sample A2A Purchase Walkthrough: Coca-Cola 12-Pack

This walkthrough follows one order from marketplace discovery through the
seller's final A2A confirmation and email. It uses the real local catalog entry
and buyer profile, and distinguishes what the current seller policy can do from
the requested $5/free-delivery outcome.

## Scenario

- Product: Coca-Cola Original Taste Soda, 12-Pack
- UPC-A: `049000028904`
- Current advertised price: `$6.99`
- Quantity: 1
- Buyer address: `123 Example Street, Example City, CA 00000`
- Buyer email: `buyer@example.com`
- Initial offer: `$1.99` per 12-pack, same-day delivery
- Preferred appointment: Saturday, November 14, 2026, at 1:00 PM San Francisco
  time
- Desired final terms: `$5.00`, delivery within 7 days, free delivery

The merchant's mock schedule is Monday-Saturday, 9:00 AM-5:00 PM, in
`America/Los_Angeles`. November 14, 2026 is a Saturday, so 1:00 PM is an
eligible slot if nobody else is holding it.

## Important: The Exact Requested Deal Is Not Available Under Today's Policy

The current [seller policy](./Config/seller_policy.json) records 60 daily sales
for this UPC. That puts it in the 50-100 sales/day tier, which allows a 10%
discount. Waiting 7 days adds 10 percentage points, making the largest combined
discount 20%.

The current policy also charges `$10` for one-item city delivery. It waives this
fee only when quantity is at least 3; waiting 7 days does not waive shipping.
Also, although negotiation supports a 0-day wait term, the scheduler always
requires delivery to be at least tomorrow. The current implementation cannot
book a same-day appointment.
As a result, with today's settings:

- Same-day request at `$1.99`: seller counter is `$6.30` per item plus `$10`
  delivery.
- `$5.00`, 7-day, free-delivery request: seller counter is `$5.60` per item
  plus `$10` delivery.

These are mock-policy calculations, not live sales data. The buyer can accept
the actual counteroffer or end negotiation, but cannot obtain `$5.00` and free
delivery for one item under the current policy.

To make the desired result possible, the seller policy would need to allow at
least a 29% combined product discount for this item and waive the delivery fee
for a one-item order. For example, changing the 50-100-sales discount from 10%
to 19% would make it 29% after the existing 7-day wait discount, allowing a
`$5.00` offer against the `$6.99` list price. Setting the free-shipping quantity
threshold to 1 would waive the fee, but would do so for all eligible city
orders, not only orders that wait 7 days. The current policy format cannot make
free delivery conditional on a wait period; that behavior would require a
code/schema change.

The sample dialogue below shows the requested successful outcome **only after
the seller policy has been intentionally adjusted**. Without that adjustment,
the counteroffers above are the actual outcomes.

## Step 1 — Discover the Product

Start the mock merchant on port `5555`, then start the buyer CLI with local test
mode enabled:

```sh
A2A_ALLOW_LOCAL_TEST_STORE=1 .venv/bin/buyer-agent
```

The buyer CLI discovers the merchant's A2A Agent Card and UCP catalog, then
uses the catalog-matching model only to find products in that catalog.

```text
Buyer: Coca-Cola regular 12-pack

Marketplace results:
1. Coca-Cola Original Taste Soda, 12-Pack
   UPC-A: 049000028904
   Price: $6.99

Buyer: select 1
Buyer: quantity 1
```

The title, UPC, price, and availability come from the merchant catalog. The
model does not invent a product or set its price.

## Step 2 — Negotiate the Price and Delivery Terms

The CLI asks whether to check for a published product discount. This UPC has no
published offer, so the buyer explicitly starts price negotiation.

```text
Buyer: check for a published discount? no
Buyer: explicitly negotiate? yes
Buyer: willing to wait? 0 days
Buyer: ask for free delivery? yes
Buyer: maximum offer per item? $1.99
```

The buyer agent sends the offer to the seller over A2A. With today's policy,
the seller returns a counteroffer of `$6.30` plus `$10` delivery. The buyer
declines that counter and makes another offer:

```text
Seller (A2A counteroffer): $6.30 each, $10.00 delivery, $16.30 total.
Buyer: accept seller's quoted terms? no
Buyer: make another offer? yes
Buyer: maximum offer per item? $5.00
Buyer: willing to wait? 7 days
Buyer: ask for free delivery? yes
```

With today's policy, this second offer is countered at `$5.60` plus `$10`
delivery. To get the requested outcome, the seller policy must first be
adjusted as described above. Under that adjusted policy, the seller can respond:

```text
Seller (A2A counteroffer to the initial request):
  $5.67 per item, $10.00 delivery, 0-day wait.

Buyer: accept? no
Buyer: make another offer? yes
Buyer: maximum offer per item? $5.00
Buyer: willing to wait? 7 days
Buyer: ask for free delivery? yes

Seller (A2A accepted offer):
  $5.00 per item, free delivery, $5.00 total, 7-day wait.

Buyer: accept the seller's quoted terms? yes
```

An offer below the seller's allowed floor is not accepted automatically: the
seller returns its permitted price. The buyer explicitly accepts the final
quote before continuing.

## Step 3 — Schedule Delivery, Then Pay

After the price quote is accepted, the CLI asks for delivery or pickup. Choose
delivery and enter the requested Saturday time:

```text
Buyer: fulfillment method? delivery
Buyer: preferred delivery date and time? 2026-11-14 13:00
```

The buyer agent sends the date/time and buyer's configured home address to the
seller over A2A. If the time is available:

```text
Seller (A2A): I can deliver at 2026-11-14T13:00 (America/Los_Angeles).
Buyer: accept this delivery appointment? yes
Seller (A2A): appointment locked for checkout.
```

If that slot is unavailable, the seller returns its nearest available slot as
an A2A counteroffer. The CLI shows the proposed date/time and asks the buyer to
accept it. If accepted, the seller locks that replacement slot; if declined,
the buyer can propose another date/time. For this scenario, the replacement
must still honor the agreed 7-day wait.

**Payment ordering in the current implementation:** the price is agreed in
Step 2, but Stripe test-mode payment occurs after the seller locks the
appointment in Step 3. The CLI displays the product, quote, delivery fee,
appointment, and address, then asks for explicit authorization:

```text
Order summary
Product: Coca-Cola Original Taste Soda, 12-Pack × 1
Sandbox total: $5.00 USD
Delivery fee: $0.00 USD
Delivery appointment: 2026-11-14T13:00 (America/Los_Angeles)
Delivery address: 1499 California street, Apt 319, San Francisco, CA, 94109

Buyer: authorize this Stripe TEST-MODE payment? yes
Buyer agent: Stripe sandbox payment confirmed.
```

The schedule hold is 30 minutes. If the buyer declines payment, the buyer agent
releases the hold. A successful sandbox payment uses the accepted A2A quote.
The initial same-day request is therefore the buyer's 0-day negotiation
preference, not a schedulable same-day appointment. The final 7-day quote makes
the requested November 14 appointment eligible.

## Step 4 — Seller Confirms Over A2A and Emails the Buyer

After payment, the buyer agent sends a final confirmation request over A2A with
the schedule ID, checkout ID, buyer email, and phone. The seller verifies that
the matching checkout was paid, then sends the final email and returns the
order confirmation over A2A.

```text
Seller (email to buyer@example.com):
  Order confirmed
  Product: Coca-Cola Original Taste Soda, 12-Pack
  UPC-A: 049000028904
  Quantity: 1
  Unit price: $5.00 USD
  Delivery fee: $0.00 USD
  Final total: $5.00 USD
  Fulfillment: delivery
  Delivery appointment: 2026-11-14T13:00 (America/Los_Angeles)
  Delivery address: 1499 California street, Apt 319, San Francisco, CA, 94109
  Agreed delivery wait: 7 days
  Delivery fee waived: yes

Seller (A2A response to buyer agent): confirmed with matching order details.
Buyer agent CLI: Seller confirmation received over A2A.
```

The merchant must have its own `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, and `SMTP_FROM_EMAIL` environment variables configured. If
SMTP is missing, scheduling is blocked before payment. If sending the email
fails after payment, the seller reports an A2A error and leaves the appointment
unconfirmed so confirmation can be retried after SMTP is fixed.

This is a mock-store and Stripe test-mode demonstration. A successful sandbox
payment and appointment do not arrange real transport or fulfill physical
goods.
