# Deployment: build, rebuild and restore the site on a VM

Use this to install the site on a new VM, to rebuild it after the VM was lost, or to move it.
Everything the **site** needs is in this repository. The **data** (people, schedule, accounts, audit log) lives only on the VM, see "Data" below.

Result: `https://<ITDUTY_HOST>` works with a trusted certificate, the access rules you chose and a nightly backup.

## 0. What you need before you start

| Item | Notes |
|------|-------|
| VM | Ubuntu Server 24.04 (not minimized), 2 vCPU, 2 GB RAM, 30 GB disk, static IP, a user with sudo (the scripts call it `SSH_USER`), OpenSSH server on |
| Settings | Your real `duty.conf` values: VM IP, SSH user, the admin PC, the allowed networks. They are **not** in this repo (it only has `duty.conf.example`); keep them in a password manager or an internal document. |
| Network | From the admin PC to the VM: TCP 22. From employee networks to the VM: TCP 80 and 443. From the VM: DNS (53), NTP (123), HTTPS (443) to the Ubuntu mirrors, `download.docker.com`, Docker Hub and PyPI (during setup and every rebuild). |
| DNS | An A record for `ITDUTY_HOST` pointing to the VM IP. |
| CA | Optional: a way to get a certificate for that name from your internal CA (see step 5). |
| Backup file | Optional: the newest `itduty-*.db` backup, if you have one (see "Data"). |

## 1. Get the files onto the VM

```bash
git clone <your-repository-url> it-duty-board
scp -r it-duty-board <ssh-user>@<vm-ip>:~/it-duty-board
ssh <ssh-user>@<vm-ip>
cd ~/it-duty-board/vm
cp duty.conf.example duty.conf
nano duty.conf            # VM_IP, SSH_USER, ITDUTY_HOST, ADMIN_SSH_NET, WEB_NETS, BLOCK_NETS, ITDUTY_ADMIN_NETS
```

`duty.conf` is ignored by git on purpose. Every script validates the networks and stops with a message if a value is wrong.

## 2. Prepare the VM (once)

```bash
sudo bash 00-prep.sh
```

Sets the time zone, installs packages, syncs time, hardens SSH (only `SSH_USER`, only from `ADMIN_SSH_NET`), enables the firewall (blocked networks denied first, then the allowed networks, SSH from the admin PC only), fail2ban and Docker.

Keep your SSH session open and test a **new** login from the admin PC before you close the first one.

## 3. Temporary certificate

```bash
sudo bash 09-cert.sh selfsigned
```

The site needs some certificate before it can start. This one is valid for a year and browsers will warn about it until step 5.

## 4. Build and start the site

```bash
sudo bash 10-deploy.sh
```

It builds the container (a few minutes), writes the nginx config, starts everything, installs the host-name helper for the Users page (`40-hostnames.sh`) and runs its own tests. At the end it prints a table of checks; every `got` must equal its `expect`:

| Check | Expected |
|-------|----------|
| `/`, `/healthz`, `/api/today`, `/static/style.css` | 200 |
| `/admin/login` (from the VM itself) | 403 |
| old name (`REDIRECT_HOSTS`) over http and https | 301 to the main name, same path |
| `http://` redirect | 301 |
| bare IP over https | 000 (connection refused on purpose) |

It also proves that the container has no network. If there are no accounts yet it creates the owner `admin` with the temporary password `admin`; the app forces a new password at the first login.

Then:

```bash
sudo bash 20-backup.sh      # first backup now + nightly job at 03:00
```

## 5. Certificate from your CA (padlock)

```bash
sudo bash 09-cert.sh csr
```

This writes `~/duty.csr`; the private key stays on the VM. Then submit the request to your CA (for a Microsoft AD CS: the web enrollment page, "advanced certificate request", template **Web Server**; if the issued certificate lacks the name, add the attribute `san:dns=<ITDUTY_HOST>`). Download the certificate as **Base 64 encoded**, copy it to the VM as `~/duty.cer` and run:

```bash
sudo bash 09-cert.sh install ~/duty.cer
```

The script checks that it matches the key, covers every name (`ITDUTY_HOST`, `EXTRA_HOSTS` and `REDIRECT_HOSTS`) and is not expired, then reloads nginx. Put the expiry date in a calendar and repeat this step before it passes.

### Renaming the site or adding a name

`ITDUTY_HOST` is the main name: the site is served under it. `REDIRECT_HOSTS` lists old names that only redirect (permanent, same path and query) to the main name; `EXTRA_HOSTS` lists further names that serve the site too. Every name must be in the certificate: run `sudo bash 09-cert.sh csr` (the request covers all of them), get the certificate, `sudo bash 09-cert.sh install ~/duty.cer`, then `sudo bash 10-deploy.sh`. Keep the DNS record of an old name pointing to the VM, otherwise nobody reaches the redirect, and add the record of a new name before announcing it. Administrators have to sign in again after a rename (the login cookie belongs to one name).

## 6. DNS

Point the A record of `ITDUTY_HOST` to the VM IP on your DNS servers, not on the VM. Clients may cache the old answer for up to the record's TTL.

## 7. Data

- **If you have a backup file** (copy it to the VM first):

  ```bash
  sudo bash 30-restore.sh /path/to/itduty-YYYYMMDD-HHMMSS.db
  ```

  It checks the file, keeps the current database as `itduty.db.pre-restore.<time>`, restores, restarts and lists the accounts. Passwords are kept; everybody has to sign in again.

- **If you have no backup**, the site starts empty:
  1. Sign in at `/admin` as `admin` / `admin` from an allowed network and choose a new password.
  2. Add people on the Staff page (or paste a list in the schedule's import).
  3. Create the other administrators (Users page, owner only); each gets a temporary password shown once.
  4. Assign the duty days again.

The nightly backup (`20-backup.sh`) keeps the newest 14 copies in `<APP_DIR>/data/backups` **on the same VM**, so they are lost with it. Copy them elsewhere regularly. `20-backup.sh` has a hook for this: set `BACKUP_COPY_CMD` in `duty.conf` to a command that receives the backup file path as `$1`.

## 8. Final checks

From a PC on an allowed network:

- `https://<ITDUTY_HOST>` opens with a padlock and shows today's duty people.
- `/admin` asks for a user name and password.
- From a blocked network or the internet: no connection.

From the VM:

```bash
sudo docker ps                              # it-duty: healthy
sudo ufw status                             # allow rules for the networks, SSH only from the admin PC
sudo docker exec it-duty flask list-users   # accounts
```

## 9. Things that are not part of the repository

- The real `duty.conf` values and the DNS record.
- Any temporary passwordless-sudo rule you used while building: it must not exist on the finished VM.
- Anything written on the old VM after the last backup.
