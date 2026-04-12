# PythonAnywhere Deploy Checklist

Ye project PythonAnywhere par chalane ke liye ye exact steps follow karo.

## 1. Bash console me project open karo

```bash
cd ~/pradhanfromfhilling
source venv/bin/activate
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate
```

## 2. `.env.local` ya environment variables set karo

Production ke liye minimum ye values chahiye:

```env
DEBUG=False
ALLOWED_HOSTS=your-username.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://your-username.pythonanywhere.com
```

Agar Cloudinary use karna hai to:

```env
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
```

## 3. PythonAnywhere Web tab settings

### Source code
- Working directory: project folder

### WSGI file
Ensure `portal_main.wsgi` load ho raha ho.

### Static files mapping
Add:

- URL: `/static/`
- Directory: `/home/YOUR_USERNAME/pradhanfromfhilling/staticfiles`

Optional local media only:
- URL: `/media/`
- Directory: `/home/YOUR_USERNAME/pradhanfromfhilling/media`

## 4. Web app reload

PythonAnywhere Web tab me `Reload` button click karo.

## 5. Live URLs test karo

Ye sab `200` dene chahiye:

- `/`
- `/manifest.json`
- `/sw.js`
- `/static/icons/icon-192.png`
- `/static/icons/icon-512.png`

## 6. PWA install test

Mobile Chrome me:

1. live site kholo
2. login karo
3. menu -> `Install app` / `Add to Home Screen`

Install ke baad hi splash/PWA mode full feel aayega.

## 7. Agar login page hi sirf chal raha ho

Usually in 4 me se ek issue hota hai:

1. `collectstatic` run nahi hua
2. `/static/` mapping galat hai
3. `DEBUG=False` + host/origin set nahi hai
4. browser purana cache pakad raha hai

## 8. Cache clear

PWA test ke liye:

1. purana installed app uninstall karo
2. browser site data clear karo
3. phir dubara site open/install karo
