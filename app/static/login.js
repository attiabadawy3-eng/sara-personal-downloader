const err=new URLSearchParams(location.search).get('error');
if(err){const field=document.getElementById('loginError');field.hidden=false;field.textContent=err==='locked'?'عدد محاولات كبير. حاول بعد 15 دقيقة.':'كلمة المرور غير صحيحة.';}
