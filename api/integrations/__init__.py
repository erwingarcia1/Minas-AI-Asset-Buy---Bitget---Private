"""Third-party integrations exposed as plain async functions.

- ``sumsub``: KYC applicants, status lookups, webhook signature checks
- ``resend``: transactional email

Each module reads its credentials from the environment at call time and
raises a typed error (``SumsubError`` / ``ResendError``) that the routes map
to 502/503. Nothing here touches the database.
"""
