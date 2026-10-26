---
title: "GitLab Password Standards"
source: https://handbook.gitlab.com/handbook/security/policies_and_standards/password-standard/
---

{{< label name="Visibility: Audit" color="#E24329" >}}

## Purpose

This document outlines information security password standards intended to protect GitLab information systems and other resources containing confidential (Red and Orange) GitLab data from unauthorized use, where technically feasible.

## Scope

Applies to all GitLab team members, contractors, advisors, and contracted parties interacting with GitLab computing resources and accessing confidential data.

## Roles & Responsibilities

| Role  | Responsibility |
|-----------|-----------|
| GitLab Team Members | Responsible for adhering to the requirements outlined in this standard |
| Security | Responsible for defining and monitoring implementation of these standards for critical applications |
| Security Management (Code Owners) | Responsible for approving significant changes and exceptions to these standards |

## Standard

Constructing secure passwords and ensuring proper password management is essential. GitLab's password standards are based, in part, on the recommendations by [NIST 800-63B](https://pages.nist.gov/800-63-3/sp800-63b.html). To learn what makes a password truly secure, read this [article](https://medium.com/peerio/how-to-build-a-billion-dollar-password-3d92568d9277) or watch this [conference presentation](https://www.youtube.com/watch?v=vudZnjp5Uq0&t=19183) on password strength.

**Note: If a system cannot support a specific configuration in this standard due to technical limitations, the configuration must be set to the closest possible setting that matches this standard. An exception must be opened [here](https://gitlab.com/gitlab-com/gl-security/security-assurance/governance-and-field-security/governance/security-governance), and an associated risk rating and exception review timeline will be assigned in accordance with the matrix in the request issue.**

### Password Requirements

- Minimum Length = 12 characters
- Special Characters = No
- Password Reuse = No
- Password expiration = No
- Multi-factor authentication (MFA) = Yes, whenever possible

To make a secure password you can remember, consider using a [combination of 5 or more random words](https://medium.com/peerio/how-to-build-a-billion-dollar-password-3d92568d9277#67c2) or [generate in 1Password](/handbook/security/corporate/systems/1password/) and store. This helps ensure the password isn't easily guessable and will be unique across sites.

### Password Management

- Passwords are to be kept private and secured.
- Individual account passwords are not to be shared.
- Passwords are not to be stored in clear text or be written down.
- Password "hints" are not to be used. If a password is forgotten, a mechanism must be in place to replace a password/passphrase with sufficient controls to verify the identity of the requester of the password reset.
- Passwords must be stored in a way that is resistant to offline attacks and must be salted and hashed using a suitable one-way key derivation function.
- If a password is required to be stored, it must be stored within an approved password or secrets manager.
- If an account or password is suspected to have been compromised, immediately report the incident to Security and promptly follow instructions.

### System Password Configuration Requirements

- For systems where a password can be configured the minimum password length needs to be set to 12 characters.
- The use of special characters is not required or even recommended.
- If a particular system requires a password history, configuration should be set for 25 remembered passwords.
- Passwords are not acceptable if they match the subsequent patterns, and must be checked against commonly used or expected patterns, including: known breached password lists, dictionary words, repetitive or sequential characters, or context specific words such as the name of the service, username, or derivatives thereof.
- System administrators of applications and/or devices must change default passwords.
- System administrators need to enable password strength on third party applications and/or tools, where applicable.
- For applications where a password is the only source of authentication, a password must be expired within a maximum of 90 calendar days.
- Systems should monitor and log failed login attempts.
- Information related to authentication failed login attempts need to be recorded within the application logs if technically feasible; such as: name, date, number of failed attempts, unique log identifier.
- Repeated failed login attempts need to trigger a temporary account lockout after 10 failed attempts. If the particular system will not support lockout after 10 attempts or less, the lockout needs to be configured to the minimum value allowed by the system. The lockout may end after a designated period of time, or require a manual unlock, depending on the profile of the application.
- [Multi Factor Authentication](https://en.wikipedia.org/wiki/Multi-factor_authentication) (MFA) must be enforced.

### Multi Factor Authentication (MFA or 2FA)

All GitLab team members are required to use [Multi-Factor Authentication](https://www.cisa.gov/resources-tools/resources/multi-factor-authentication-mfa) (MFA). Usage of MFA by GitLab team members is **required** for access to the production environment.

The table below ranks authenticator types by assurance level. 
Not all MFA methods offer equivalent protection. The key security distinction is **phishing resistance**: whether authentication is cryptographically bound to the relying party origin, making credentials impossible to replay against a spoofed site. 

GitLab team members should use the strongest method available to them.

| Assurance | Authenticator | Examples | Factor(s) (Have / Know / Are) | Phishing-Resistant | Hardware-Protected | Device-Bound | User-Verifying | Vulnerable To |
|---|---|---|---|:---:|:---:|:---:|:---:|---|
| **High** | FIDO2 Roaming Authenticator | YubiKey (FIDO2 mode), Google Titan | Have + Are or Know | ✅ | ✅ | ✅ | ✅ | Physical theft or loss |
| **High** | FIDO2 Platform Authenticator | Touch ID, Face ID, Windows Hello, Android Fingerprint | Have + Are or Know | ✅ | ✅ | ✅ | ✅ | Device compromise, physical access |
| **High** | Okta FastPass (biometric or PIN) | Okta Verify + FastPass | Have + Are or Know | ✅ | ✅ ¹ | ✅ | ✅ | Device compromise; device-presence-only mode does not satisfy MFA |
| **High** | Synced Passkey | 1Password, iCloud Keychain, Chrome/Google Password Manager | Have + Are or Know ² | ✅ | ❌ | ❌ | ✅ | Sync account or vault compromise |
| **Medium** | Push Notification | Okta Verify Push | Have (+ Are) ³ | ❌ | ✅ ¹ | — | ✅ | AiTM phishing, MFA fatigue (push bombing) |
| **Medium** | Hardware OTP Token | YubiKey OTP mode, RSA SecurID | Have | ❌ | ✅ | ✅ | ❌ | AiTM real-time phishing, physical loss |
| **Low** | TOTP / Soft Token | Okta Verify OTP, Google Authenticator, 1Password TOTP | Have | ❌ | ❌ | ❌ | ❌ | AiTM real-time phishing, seed theft |
| **Restricted** | SMS / Voice | — | Have | ❌ | ❌ | ❌ | ❌ | SIM swapping, SS7 interception, social engineering |
| **Not Permitted** | Security Question | Secret questions, account recovery prompts | Know | ❌ | ❌ | ❌ | ❌ | Guessing, social engineering, data breach exposure |
| **Not Permitted** | Password Hint | — | Know | ❌ | ❌ | ❌ | ❌ | Guessing, social engineering; by design reduces password entropy |

¹ Hardware protection is device-dependent; requires Okta Verify enrollment with Device Trust enabled.  
² The passkey is Have; the vault or sync account protecting it requires Are or Know. The combined assurance depends on how that account is secured.  
³ Biometric activation of the push approval is device and configuration dependent.

> **Note on biometrics:** Biometric prompts (Touch ID, Face ID) are activation mechanisms — they unlock the cryptographic authenticator. They are not authenticators themselves.

For a better understanding of how MFA fits into GitLab, refer to the [Accounts and Passwords](/handbook/security/password-guidelines/) section, which includes pointers for setting up passwords, acquiring FIDO2 tokens, and links to further resources. Refer to the Tools and Tips page for more detailed information regarding [FIDO2/WebAuthn](/handbook/tools-and-tips/#fido2--webauthn) and [other 2FA methods](/handbook/tools-and-tips/#other-2fa-methods).

#### Application Authentication Requirements

- Effective FY23 Q3, all third party applications that house GitLab confidential data are required to [authenticate via Okta inline with GitLab's approach to centralized authentication and authorization](/handbook/eta/corporate-it/end-user-services/okta/#what-is-okta). [Security Notices](/handbook/security/security-assurance/security-risk/third-party-risk-management/#tprm-security-notice-process) will be required in all cases where Okta is not supported.
- Authentication to an application should contain multi-factor authentication (Token, OTP Generator, SSO, YubiKey).
- OIDC, SAML, WS-Federation after logging into an authentication portal is required where technically feasible (e.g. Okta).
- Authentication to an application should support individual users, not groups.

## Exceptions

Exceptions to this standard will be tracked as per the [Information Security Policy Exception Management Process](/handbook/security/controlled-document-procedure/#exceptions).

## References

None
