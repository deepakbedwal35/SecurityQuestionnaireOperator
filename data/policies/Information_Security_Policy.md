# Northwind Systems - Information Security Policy
**Effective Date:** 2024-01-01  
**Version:** 3.2  
**Owner:** CISO Office  

## 1. Access Control & Identity
- All production access requires Multi-Factor Authentication (MFA) via FIDO2 WebAuthn hardware keys or TOTP. SMS MFA is explicitly prohibited.
- Single Sign-On (SSO) using SAML 2.0 / OIDC is mandatory for all enterprise applications.
- Passwords must be at least 16 characters and checked against breached password databases.
- Access reviews are conducted quarterly by department heads.

## 2. Encryption Standards
- Data at Rest: Encrypted using AES-256 with AWS KMS customer-managed keys (CMKs) rotated annually.
- Data in Transit: TLS 1.3 enforced across all public endpoints; TLS 1.2 minimum for internal service-to-service communication. Weak ciphers (RC4, DES, 3DES) are disabled.

## 3. Vulnerability Management & Penetration Testing
- Third-party external penetration tests are conducted annually by independent CREST-certified assessors.
- Static Application Security Testing (SAST) and Software Composition Analysis (SCA) run on every git commit.
- Critical vulnerabilities must be patched within 7 days; High vulnerabilities within 30 days.

## 4. Employee Training & Background Checks
- All employees undergo standard criminal background checks prior to employment.
- Security awareness training is mandatory upon hire and refreshed annually with monthly phishing simulations.
