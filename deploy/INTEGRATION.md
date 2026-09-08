# SandMan Watchdog — Host App Integration Guide

## How username is resolved

The watchdog reads the username in this priority order:

```
1. URL parameter   → https://watchdog.staging.com/?user=aiauser
2. Cookie          → sandman_user=aiauser  (if same domain/subdomain)
3. window.userName → injected by host app before page loads
4. Empty string    → shows "not found" error
```

## Recommended: URL parameter (cross-origin safe)

Your Java/Spring host app already knows the logged-in user from JSESSIONID.
When redirecting to the watchdog, append `?user=<username>`:

```java
// Spring Boot example
@GetMapping("/monitoring")
public String openWatchdog(HttpSession session) {
    String username = (String) session.getAttribute("username");
    // OR from Spring Security:
    // String username = SecurityContextHolder.getContext().getAuthentication().getName();
    
    String watchdogUrl = "https://watchdog.staging.com/?user=" + username;
    return "redirect:" + watchdogUrl;
}
```

```javascript
// JavaScript / React example
const username = getCurrentUser(); // from your auth context
const watchdogUrl = `https://watchdog.staging.com/?user=${encodeURIComponent(username)}`;
window.open(watchdogUrl, '_blank');
// OR embed as iframe:
// <iframe src={watchdogUrl} />
```

## Alternative: Same-domain cookie

If both apps are on the same domain (e.g. `app.company.com` and `watchdog.company.com`),
set a cookie from the host app:

```java
// Set cookie when user logs in
Cookie cookie = new Cookie("sandman_user", username);
cookie.setDomain(".company.com");  // shared across subdomains
cookie.setPath("/");
cookie.setMaxAge(3600);
response.addCookie(cookie);
```

The watchdog will automatically read `sandman_user` from the browser cookie.

## Username → Foundry mapping

Usernames are mapped to foundries via the central `sandman_dev` database:

```sql
users.user_name → customers.db_properties → foundry DB name
```

The watchdog resolves this automatically. If a user is not found, the UI shows
a clear "User not found" error with a red indicator in the header.

## Staging URL format

```
https://watchdog.staging.com/?user=<sandman_username>
```

Examples:
- GPI foundry:    `?user=aiauser`
- Munjal foundry: `?user=munjalkiriu1`
- All Munjal users: `munjalkiriu1`, `laboratory`, `castingstd`, `MKIPL`
