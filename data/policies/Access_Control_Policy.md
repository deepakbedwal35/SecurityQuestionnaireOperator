# Northwind Systems - Access Control Policy
**Effective Date:** 2024-01-10  
**Version:** 2.1  
**Owner:** IT & Infrastructure  

## 1. Principle of Least Privilege
- Role-Based Access Control (RBAC) and Just-in-Time (JIT) access are enforced across all production environments.
- Direct SSH/RDP to production instances is disabled; all access goes through an audited Teleport bastion with session recording.
- Offboarding: System access revoked within 24 hours of employee departure.
