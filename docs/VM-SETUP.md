# Running the platform in a VM (VMware Fusion, Intel Mac)

This sets up a test VM on an Intel Mac using Wi-Fi only. The same steps work
in VMware Workstation or VirtualBox on a staff laptop.

## 1. Download Ubuntu Server

Get **Ubuntu Server 24.04 LTS, amd64** (`ubuntu-24.04.x-live-server-amd64.iso`)
from ubuntu.com/download/server. It's about 2.7 GB.

## 2. Create the VM in Fusion

1. File → New → drag the ISO in → Continue.
2. If Fusion offers "Easy Install", **turn it off**: it may install a desktop.
3. Customize Settings before starting:
   - Processors & Memory: **4 cores, 10240 MB**. That leaves 6 GB for macOS on a 16 GB Mac.
   - Hard Disk: **60 GB**.
   - Network Adapter: **Share with my Mac** (NAT) for now.
4. Save as `ctf-vm` and start it.

## 3. Install Ubuntu Server

In the installer, accept the defaults except:

- **Network**: the adapter should get a DHCP address like `172.16.x.x`. Leave it.
- **Storage**: "Use an entire disk". **Turn off "Set up this disk as an LVM
  group"**, or keep LVM but resize the root volume to use the full disk (by
  default Ubuntu gives `/` only half of an LVM disk).
- **Profile**: user `ctf`, server name `ctf-vm`, and a password you'll remember.
- **SSH**: tick **Install OpenSSH server**.
- **Featured snaps**: select none.

Reboot when asked (press Enter if it says to remove the install medium) and
log in on the console.

## 4. From now on, use SSH from the Mac

On the VM console:

```bash
ip -4 addr show | grep inet
```

Note the `172.16.x.x` address. Then, in Terminal on the Mac:

```bash
ssh ctf@172.16.x.x
```

SSH lets you copy and paste. Do all remaining steps over SSH.

## 5. Prepare the VM

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y git make
df -h /          # should show ~55-60 GB; if ~28 GB, see "LVM" below
free -g          # should show ~9-10 GB
```

**LVM:** if `/` is about half the disk, extend it:

```bash
sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv && sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
```

**Swap:** turn it off. Kubernetes memory limits are only predictable
without swap. If memory runs out, a challenge pod gets OOM-killed instead of
the whole VM slowing to a crawl.

```bash
sudo swapoff -a && sudo sed -i '/swap.img/s/^/#/' /etc/fstab && sudo rm -f /swap.img
```

Ignore the "New release 26.04 available" message. Stay on 24.04, the
version the platform is tested on.

## 6. Get the project (private repo)

```bash
sudo apt-get install -y gh
gh auth login          # GitHub.com → SSH → generate a key → Login with a web browser
gh repo clone oubellaismail/local-ctf
cd local-ctf
```

`gh` shows a one-time code. Open the link it prints in a browser on your Mac,
enter the code, and sign in there. Later, `git pull` picks up fixes.

## 7. Snapshot

Shut down the VM (`sudo poweroff`). In Fusion, go to Virtual Machine →
Snapshots → Take Snapshot, and name it **clean-ubuntu**. If a test breaks the
VM, you can go back to this snapshot instead of reinstalling.

Start the VM again and SSH back in.

## 8. Keep the Mac awake

In a separate Mac Terminal window, leave this running during tests:

```bash
caffeinate -dims
```

## 9. Run the dry run

Follow [DRY-RUN.md](DRY-RUN.md) starting at **Phase 1**, with these changes for the VM:

- `CTF_HOST` = the VM's `172.16.x.x` address.
- `make install` disables sleep inside the VM, which is harmless. The Mac stays awake because of step 8.
- "Player device" = **Safari or Chrome on your Mac**. With NAT, only the Mac can reach the VM.
- **Skip phase 0 and step 5.2's phone check for now.** They need bridged mode (below).
- After Phase 2 passes, take another snapshot: **platform-deployed**.

## 10. Bridged mode (other devices reach the VM)

1. Take a snapshot first (e.g. **before-bridged**).
2. Shut down the VM → Settings → Network Adapter → **Bridged → Wi-Fi** (or
   Ethernet). Start it.
3. On the VM console: `ip -4 addr show ens33 | grep inet`. It should now have
   an address in the **same range as your Mac's** (`ipconfig getifaddr en0` on
   the Mac).
   - **No address**, or only a `169.254.x.x` one: the network doesn't accept
     bridged VMs (common on campus Wi-Fi that allows one device per login).
     Go back to NAT; the event laptops will need native Ubuntu or Ethernet.
4. SSH to the new address, then move the platform to it:
   ```bash
   cd ~/local-ctf && make reip
   ```
   About 3 minutes: k3s restarts, the certificate is reissued (same CA), and
   the controller gets the new address for instance URLs.
5. `make status`, then open `https://<new IP>` from the Mac **and a phone**
   on the same Wi-Fi (dry-run steps 0.2 and 5.2).

To go back to NAT later: switch the adapter back, boot, `make reip` again.

## 11. A second VM as the challenge node (dry-run Phase 8)

No second laptop? A second VM on the same Mac works the same way.

**Memory:** a 16 GB Mac can't run 10 GB + 4 GB VMs and macOS comfortably.
Shut down `ctf-vm` and lower it to **8 GB**; the agent gets **4 GB**.

1. **Clone:** in Fusion, select `ctf-vm` → Snapshots → **clean-ubuntu** →
   *Create Full Clone*, named `ctf-agent`. (Plain Ubuntu, no k3s yet.)
2. **Settings for `ctf-agent`:** 2 cores, 4096 MB, Network: **Bridged → Wi-Fi**
   (same as `ctf-vm`, so they can reach each other).
3. **Make the clone unique.** On its console:
   ```bash
   sudo hostnamectl set-hostname ctf-agent-1
   sudo rm -f /etc/machine-id && sudo systemd-machine-id-setup
   sudo rm -f /etc/ssh/ssh_host_* && sudo dpkg-reconfigure openssh-server
   sudo reboot
   ```
   The machine-id matters: DHCP uses it to identify the machine, so two
   VMs with the same one can be given the **same IP**.
4. After the reboot: `ip -4 addr show ens33 | grep inet` should show its own
   address, different from `ctf-vm`'s.
5. **Join.** On `ctf-vm`:
   ```bash
   cd ~/local-ctf && make join-script
   scp .secrets/join-agent.sh ctf@<agent IP>:
   ```
   On `ctf-agent-1`:
   ```bash
   sudo bash join-agent.sh
   ```
6. Continue with [DRY-RUN.md Phase 8](DRY-RUN.md). For 8.4 ("unplug laptop
   B"), use Fusion → Virtual Machine → Network Adapter → **Disconnect** on
   `ctf-agent`.
