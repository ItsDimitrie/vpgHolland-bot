# VPG Transfer Discord Bot

A lightweight Discord bot that monitors the Virtual Pro Gaming **Holland Movement** feed and posts new transfers to a Discord channel as embedded messages.

---

## Features
- Polls the VPG API every 20 seconds for new player transfers.  
- Sends a clean embedded message with club names, fee, and timestamp.  
- Automatically skips duplicates.  
- Uses a `.env` file for secrets and persists the last processed transfer ID.  

---

## Requirements
- Python **3.10+**
- A Discord bot application with a valid bot token.  
- Permissions: **Send Messages** in the target channel.

---

<img width="859" height="970" alt="image" src="https://github.com/user-attachments/assets/32108a38-d18c-4d03-a145-2c525b0a9f30" />
