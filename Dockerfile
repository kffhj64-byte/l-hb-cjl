# استخدام صورة رسمية من Playwright تحتوي على بايثون وكل متطلبات المتصفحات
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# تعيين مجلد العمل
WORKDIR /app

# نسخ ملف المتطلبات وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# إنشاء المجلدات المطلوبة لتجنب أخطاء المسارات
RUN mkdir -p media backups

# فتح المنفذ الخاص بلوحة التحكم
EXPOSE 3000

# أمر تشغيل البوت
CMD ["python", "bot.py"]