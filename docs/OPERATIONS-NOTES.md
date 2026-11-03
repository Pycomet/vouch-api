# Operations notes: known gaps after Milestone 2

These are the limits of the M2 code that operations must work around, and what has to exist before real providers are switched on. Each item names the code involved. The schema is in [`SCHEMA.md`](SCHEMA.md) and the API in [`API_CONTRACT.md`](API_CONTRACT.md).

## 1. Manual operations in M2

- **Refunds are recorded, not executed.** Every return of money to a renter writes a `payments` row with `type = 'refund'`, `status = 'pending'`, and `metadata = {"reason", "refund_of"}`. That covers paid cancellations, no-shows, deposit releases, the rest of a deducted deposit, and late, wrong-amount or duplicate payments. The ledger has already posted the money as gone (`cr platform_cash`). Ops must send each pending refund through the provider dashboard. No code moves these rows past `pending` (`app/services/payments.py`).
- **Payouts are not initiated.** Nothing in M2 starts a vendor transfer. Ops pays vendors from the provider dashboard. The provider's `payout_completed` webhook then posts the `payout` group and records the `payout` row as paid. `GET /vendor/home` `next_payout` shows what is still owed. A payout for a booking that has not completed (and is not lost) is posted anyway, because the money has left, but it raises `ops.payout_unexpected`.
- **Refused money events are not ledgered.** When a verified event is refused, its handler's savepoint rolls back and only the `webhook_events` row is kept, with a `processing_error`, answered 200. For these events real money may have moved with no ledger entry:
  - `payment_captured` whose reference matches no `booking` payment row, or whose booking does not exist (`payment_not_found`, `booking_not_found`): money arrived and is not in the ledger. Return it by hand.
  - `payout_completed` refused with `booking_not_found`, `payout_not_found`, `payout_already_paid`, `amount_mismatch`, `recipient_missing` or `payout_exceeds_payable` (`_payout_completed` in `app/services/webhooks.py`): the transfer has already left, but nothing is posted and no payout row is recorded or updated.
  - `malformed_event` (the body did not parse) and `unsupported_event` (an unknown type): either may be a money event the API could not read.

  Ops must reconcile every row from `SELECT … FROM webhook_events WHERE processing_error IS NOT NULL` against the provider's statement of collections and transfers. Ops then returns, re-posts or writes off each one, outside the API. (`amount_mismatch`, `payment_already_captured` and guard codes on `payment_captured` are the exception: that money is already ledgered and a pending refund row exists, see §1 above.) Every `processing_error` value is listed in `SCHEMA.md` §10.
- **Marking an item lost has no route.** The `mark_lost` transition (admin, from the 7th business day after `ends_on`) exists in `app/services/booking.py`, but no HTTP endpoint exposes it. Ops runs it from a shell with an admin actor.
- **Listings and availability blocks** have no vendor or admin endpoints yet. They are written directly in the database in Phase 0.

## 2. Known technical gaps

- **Notifications are sent before commit; there is no outbox.** The booking engine calls the notifier after its flush and before the caller commits. If that commit then fails, the notification has already gone out for a change that never landed. Only the timer job retries: its next run repeats the transition and sends the notification again, so timer notifications are at least once and may be duplicated. For API routes and webhooks whose commit fails, the notification has gone out and nothing retries the change (the client or the provider may retry the request). M2 ships only the logging notifier, so nothing reaches users yet. A transactional outbox is needed before a real channel is connected (`app/services/timers.py`, `app/services/notify.py`).
- **Provider calls have no timeout.** `accept` calls `PaymentProvider.create_virtual_account` while it holds the booking row lock (`SELECT … FOR UPDATE`). A slow provider holds the lock, and the request, for as long as the call takes. Real adapters need bounded timeouts and retries; a timeout must map to 502 `payment_provider_error`, which changes nothing.
- **SMS rate limiting is per phone only.** OTP requests are limited to 5 per phone per rolling hour (`app/services/auth.py`). There is no per-IP, per-network or global limit, so a caller cycling through phone numbers can generate unbounded SMS traffic once a real sender exists. Add those limits, or take them from the SMS provider, before the real sender goes live.
- **ORM drift on `handover_records.vendor_signer_id`.** The model declares a plain FK to `users(id)` that no migration creates. The composite FK to `bookings(id, vendor_id)` already enforces the same rule. A future migration should either add the FK or remove it from the model.

## 3. Before real adapters (and a working staging)

- **Staging needs real providers.** Outside development and test, `fake` is refused for OTP, KYC and payments, and nothing else exists. So staging (like production) answers 503 for OTP requests, KYC and booking acceptance. Staging cannot run an end-to-end booking until real SMS, KYC and payment adapters are configured, against the providers' sandbox accounts.
- **Each adapter must implement its interface** (`app/providers/otp.py`, `kyc.py`, `payment.py`) and be registered (`app/providers/registry.py`, and the OTP and KYC builders). The settings types must be widened too: `otp_sender`, `kyc_provider` and `payment_provider` are `Literal["fake"] | None` in `app/core/config.py`, so another value is refused at start-up until a new literal is added. It needs:
  - a bounded timeout, retries, and errors mapped to the existing codes;
  - webhook signature verification with the provider's own scheme, in place of the fake's `X-Vouch-Signature`;
  - deterministic references, so a retried call never charges or pays twice.
- **Refund and payout execution.** Add a provider call for refunds (spec §4 has none) and payout initiation. Each needs a deterministic reference and a webhook that confirms the outcome, so the pending `refund` rows and the payouts above stop being manual.
- **Reconciliation.** Before automation, compare provider settlements and transfers with the ledger daily, by reference, amount and booking.

## 4. Placeholder policy values (pending legal advice)

These are settings, not decisions. Each booking freezes them into `price_snapshot` at request time, so a change affects only later requests.

| Setting | Placeholder | Source |
|---|---|---|
| `VOUCH_COMMISSION_RATE` | `0.15` of the rental, charged to the vendor | A8 |
| `VOUCH_DAMAGE_CONTRIBUTION_RATE` | `0.05` of the rental, optional for the renter | A8 |
| Renter cancellation before the start date | Free until payment (A13 withdrawal). After payment, a full refund less the fee below. | A8, A13 |
| `VOUCH_RENTER_CANCELLATION_FEE_RATE` | `0.10` of the rental | A13 |
| `VOUCH_CANCELLATION_FEE_VENDOR_SHARE` | `0.50` of that fee to the vendor, the rest to Vouch | A13 |
| Delivery fee | `assets.fulfilment.delivery_fee`, default `0` | A8 |

The wording of the deposit hold and the damage contribution, and the legal structure of the funds flow, are also still to be confirmed with counsel and the payment provider.
