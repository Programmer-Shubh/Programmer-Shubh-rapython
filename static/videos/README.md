# Tutorial Video

## How to Add Your Tutorial Video

### Step 1: Record Your Video
Use **OBS Studio** (free) to record your screen:
1. Download from https://obsproject.com
2. Open OBS → Settings → Output → Recording → MP4 format
3. Start Recording → Use your website step by step
4. Stop Recording → File saved to `Videos` folder

### Step 2: Add Voice (Loud Audio)
Use **CapCut** or **Canva** (free) to add voiceover:
1. Import your recorded video
2. Record voiceover (speak loudly and clearly)
3. Export as MP4

### Step 3: Upload to This Folder
Rename your video to `tutorial.mp4` and place it here.

### Step 4: Push to GitHub
```bash
git add static/videos/tutorial.mp4
git commit -m "Add tutorial video"
git push origin main
```

Render will auto-deploy and the video will appear on your dashboard!

## File Structure
```
static/videos/
  tutorial.mp4    <-- Your video file goes here
  README.md       <-- This file
```
