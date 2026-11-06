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

## 10. Later: bridged mode (other devices reach the VM)

Shut the VM down → Settings → Network Adapter → **Bridged → Wi-Fi**. Start it, and
check `ip -4 addr`. It should now have a campus IP (same range as your Mac's).

- If it gets **no IP**, or another device can't ping it, the campus Wi-Fi
  doesn't allow bridged VMs. Then the event laptops have to run Ubuntu natively (no VM) or use Ethernet.
- If it works, the IP changed, so `CTF_HOST`, the certificate and the k3s
  node IP need updating (`make reip`, not written yet). Until then, revert to
  the **clean-ubuntu** snapshot and redo the install with the new IP.
