-- Ledger health check (M3a §2.3). Returns ZERO rows when the ledger is healthy.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/ledger_check.sql
--
-- Each row is one problem: (problem, ref, detail).
--
-- (a) unbalanced_txn_group: a txn_group whose debits differ from its credits.
--     The deferred trigger trg_ledger_balanced already refuses this at COMMIT;
--     this catches a restore of damaged data or a disabled trigger.
--
-- (b) booking_balance_mismatch: a booking whose booking-scoped balances in two
--     accounts differ from what the docs/SCHEMA.md §8 recipes leave behind in its
--     current status. Balances are credit-normal (sum(cr) - sum(dr)); D and C are
--     the booking's price_snapshot "deposit" and "damage_contribution".
--
--       status                                          deposit liability   protection pool
--       deposit_paid, confirmed, in_progress,
--         returned, disputed                            D                   C
--       completed, lost                                 0                   C
--       every other status (never paid, or paid and
--         cancelled, which reverses everything)         0                   0
--
--     Late, mismatched and duplicate payments post a group and its reversal, so
--     they net to zero and do not change these figures. vendor_payable,
--     platform_revenue and platform_cash are not checked: payouts and the
--     cancellation-fee split make their expected balances depend on more than
--     the status. Bookings backfilled by migration 0003 (price_snapshot basis
--     "legacy") have no postings and are skipped.
WITH group_totals AS (
    SELECT
        txn_group,
        coalesce(sum(amount) FILTER (WHERE direction = 'dr'), 0) AS debits,
        coalesce(sum(amount) FILTER (WHERE direction = 'cr'), 0) AS credits
    FROM ledger_entries
    GROUP BY txn_group
),
booking_balances AS (
    SELECT
        booking_id,
        coalesce(sum(CASE WHEN direction = 'cr' THEN amount ELSE -amount END)
            FILTER (WHERE account = 'renter_deposit_liability'), 0) AS deposit_liability,
        coalesce(sum(CASE WHEN direction = 'cr' THEN amount ELSE -amount END)
            FILTER (WHERE account = 'protection_pool'), 0) AS protection_pool
    FROM ledger_entries
    WHERE booking_id IS NOT NULL
    GROUP BY booking_id
),
booking_expected AS (
    SELECT
        b.id,
        b.status::text AS status,
        CASE
            WHEN b.status::text IN ('deposit_paid', 'confirmed', 'in_progress', 'returned', 'disputed')
                THEN (b.price_snapshot ->> 'deposit')::numeric(12, 2)
            ELSE 0
        END AS deposit_liability,
        CASE
            WHEN b.status::text IN (
                'deposit_paid', 'confirmed', 'in_progress', 'returned', 'disputed',
                'completed', 'lost'
            )
                THEN coalesce((b.price_snapshot ->> 'damage_contribution')::numeric(12, 2), 0)
            ELSE 0
        END AS protection_pool
    FROM bookings AS b
    WHERE coalesce(b.price_snapshot ->> 'basis', '') <> 'legacy'
)
SELECT
    'unbalanced_txn_group' AS problem,
    g.txn_group::text AS ref,
    format('debits %s, credits %s', g.debits, g.credits) AS detail
FROM group_totals AS g
WHERE g.debits <> g.credits
UNION ALL
SELECT
    'booking_balance_mismatch' AS problem,
    e.id::text AS ref,
    format(
        'status %s: renter_deposit_liability %s (expected %s), protection_pool %s (expected %s)',
        e.status,
        coalesce(a.deposit_liability, 0),
        e.deposit_liability,
        coalesce(a.protection_pool, 0),
        e.protection_pool
    ) AS detail
FROM booking_expected AS e
LEFT JOIN booking_balances AS a ON a.booking_id = e.id
WHERE coalesce(a.deposit_liability, 0) <> e.deposit_liability
   OR coalesce(a.protection_pool, 0) <> e.protection_pool
ORDER BY problem, ref;
