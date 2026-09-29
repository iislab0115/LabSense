# 📊 HAR-DataLogger (GIST IISL)

**High-resolution Human Activity Recognition (HAR) data collection & automated refinement system.**

This project is a specialized tool to construct high-quality Ground Truth (GT) datasets. It features a robust 3-tier data strategy to ensure data integrity and transparency for advanced research in human activity analysis.

---

## ✨ Key Features
* **3-Tier Data Strategy**: Automatically refines raw sensor-adjacent logs into structured research datasets.
* **Real-time Monitoring**: A web-based dashboard allows researchers to monitor active sessions and live event feeds via Tailscale.
* **Auto-scrolling Console**: Features a terminal-style live log feed that automatically stays updated with the latest events.
* **Easy Distribution**: Includes a built-in **Download** button for team members to instantly access the integrated daily logs.
* **QR Code Access**: Generate a QR code instantly for seamless mobile logging.

---

## 🔬 Data Strategy: 3-Tier Output
To ensure reliable research results, the system categorizes all collected data into three distinct layers:

| Tier | File Format | Description |
| :--- | :--- | :--- |
| **Tier 1: Raw** | `raw_timeseries_*.csv` | The "Source of Truth" containing every individual start/end event. |
| **Tier 2: Refined** | `timeseries_preprocessed_*.csv` | Cleaned data with noise filtering (removes logs < 2s) and time alignment. |
| **Tier 3: Merged** | `merged_*.csv` | Episode-level summaries based on a 30s synchronization threshold. |

---

## 📢 Tutorial: How to Record Data
Follow these steps to participate in the data collection process.

### 1. Network Connection (Tailscale)
You must be on the lab's private network to access the logger.
* **Install Tailscale**: Download the app on your smartphone or PC.
* **Login**: Use the lab's shared account (Check **Notion** for credentials).
* **Enable VPN**: Ensure the **'VPN' icon** is visible at the top of your screen.

### 2. Access the Logger
Open your mobile or PC browser and enter the server address.
> **URL**: `http://<server_ip>:5000`

* **Note**: Please contact the **Project Administrator** to get the current server IP address.
* The server IP is typically assigned via the Tailscale network.

### 3. Activity Logging Method
* **Identify**: Enter your English name or ID to start.
* **Recording**:
    * **START**: Tap the activity you are currently performing. The button color will change to indicate it is active (**ON**).
    * **END**: Tap the button again once the activity is finished (**OFF**).
* **Multi-tasking**: You can select multiple activities simultaneously (e.g., *Working* while *Meeting*).
* **TRANSITION Button**: Use this when moving locations or completely changing tasks.
    * It instantly stops all current logs and resets your status to **IDLE**.

---

## 📦 Download & Execution (For Lab Members)
If you want to run the logger without setting up a Python environment, follow these steps:

1. **Download**: Go to the [Releases](https://github.com/jyoung531/HAR-DataLogger/releases) page.
2. **Get the File**: Download the latest version of `NRF_DataLogger.exe`.
3. **Run**: Double-click the `.exe` file.
    * *Note: Your PC must be connected to the lab's Tailscale network to host or access the server.*
---


👤 Author
Juyoung Lee — Master's Student, GIST AI Graduate School
Intelligent Information Systems Lab (IISL), GIST
