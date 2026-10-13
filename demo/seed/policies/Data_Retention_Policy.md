# Northwind Systems - Data Retention Policy
**Effective Date:** 2024-01-01  
**Version:** 2.0 (CURRENT AUTHORITATIVE)  
**Owner:** Data Governance Committee  

## 1. Scope and Authority
This document supersedes Data Retention Policy v1.0. The retention schedule defined herein is authoritative for all audits and compliance reviews.

## 2. Retention Schedules
- **Audit and Access Logs:** Retained for exactly 30 days in hot storage (Elasticsearch/OpenSearch) and archived to encrypted Glacier cold storage for 365 days for regulatory audit. Notice: v1 specified 90 days, but v2 reduced hot retention to 30 days.
- **Customer Application Data:** Stored for the duration of the customer contract and permanently purged within 30 days of contract termination via cryptographic erasure.
- **Database Backups:** Daily automated snapshots retained for 35 days with point-in-time recovery (PITR).
