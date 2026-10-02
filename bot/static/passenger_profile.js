(() => {
  'use strict';
  let csrf = '';
  let context = null;
  let confirmationToken = '';
  let paymentUrl = '';
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = id => document.getElementById(id);
  const setStatus = (id, text, kind='') => { const el=$(id); el.textContent=text; el.className='status '+kind; };

  const api = async (url, options={}) => {
    options.headers = Object.assign({'Content-Type':'application/json'}, options.headers || {});
    if (options.method && options.method !== 'GET' && url !== 'api/session/exchange') options.headers['X-CSRF-Token'] = csrf;
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.detail || 'Ошибка защищённого сервиса');
    return data;
  };

  const fmtDate = value => value ? new Date(value.length===10 ? value+'T00:00:00' : value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:value.length===10?undefined:'short'}) : '—';
  const addLine = (box, label, value) => { const div=document.createElement('div'); const b=document.createElement('b'); b.textContent=label+': '; div.appendChild(b); div.appendChild(document.createTextNode(value ?? '—')); box.appendChild(div); };

  function renderMasked(profile) {
    const box=$('maskedSummary'); box.textContent='';
    if (!profile) { $('existingCard').classList.add('hidden'); return; }
    addLine(box,'Пассажир',profile.name);
    addLine(box,'Год рождения',profile.birth_year);
    addLine(box,'Документ',profile.document);
    addLine(box,'Действует до',profile.expires_at);
    addLine(box,'Email',profile.email);
    addLine(box,'Телефон',profile.phone);
    $('existingCard').classList.remove('hidden');
    $('deleteBtn').classList.remove('hidden');
    $('profileHeading').textContent='Заменить данные профиля';
  }

  function formData() {
    const form=$('profileForm');
    return {
      first_name:form.elements.first_name.value,
      last_name:form.elements.last_name.value,
      middle_name:form.elements.middle_name.value || null,
      birthday:form.elements.birthday.value,
      sex:form.elements.sex.value,
      document_type:form.elements.document_type.value,
      document_number:form.elements.document_number.value,
      expires_at:form.elements.expires_at.value,
      email:form.elements.email.value,
      phone:form.elements.phone.value,
      storage_consent:form.elements.storage_consent.checked
    };
  }

  function renderTrip(summary) {
    const box=$('tripSummary'); box.textContent='';
    addLine(box,'Маршрут',(summary.from || '—')+' → '+(summary.to || '—'));
    addLine(box,'Вылет',fmtDate(summary.departure_at));
    addLine(box,'Прилёт',fmtDate(summary.arrival_at));
    addLine(box,'Класс',summary.service_class || 'Y');
    addLine(box,'Полная цена',String(summary.price_amount ?? '—')+' '+(summary.price_currency || 'RUB'));
    (summary.segments || []).forEach((s,i) => addLine(box,'Сегмент '+(i+1),`${s.from || '—'} → ${s.to || '—'} · ${s.flight || 'рейс не указан'} · ${fmtDate(s.departure_at)}`));
  }

  function renderPassenger(profile) {
    const box=$('passengerSummary'); box.textContent='';
    if (!profile) { addLine(box,'Профиль','Не заполнен — сохраните данные выше'); return; }
    addLine(box,'Пассажир',profile.name);
    addLine(box,'Год рождения',profile.birth_year);
    addLine(box,'Документ','Загранпаспорт '+profile.document);
    addLine(box,'Действует до',profile.expires_at);
    addLine(box,'Email',profile.email);
    addLine(box,'Телефон',profile.phone);
  }

  function showPayment(result) {
    paymentUrl=result.pay_url;
    $('paymentBox').classList.remove('hidden');
    $('payLink').href=paymentUrl;
    setStatus('bookingStatus','Бронь создана. Ссылка оплаты готова.','ok');
  }

  async function loadContext() {
    csrf=(await api('api/session/csrf',{method:'POST',body:'{}'})).csrf;
    context=await api('api/context',{method:'GET'});
    renderMasked(context.profile_masked);
    $('loading').classList.add('hidden');
    $('profileCard').classList.remove('hidden');
    if (context.purpose !== 'booking') return;
    $('bookingCard').classList.remove('hidden');
    renderTrip(context.offer_summary || {});
    if (context.booking_state === 'SUCCEEDED') { showPayment(context.booking_result); return; }
    if (context.booking_state === 'UNKNOWN') {
      renderPassenger(null);
      $('bookingConsent').disabled=true;
      setStatus('bookingStatus','Результат запроса к Туту не определён. Повтор заблокирован, чтобы не создать дубль. Проверьте email и обратитесь в поддержку.','error');
      return;
    }
    if (context.booking_state === 'NEEDS_PROFILE') {
      renderPassenger(null);
      setStatus('bookingStatus','Сначала сохраните профиль пассажира выше.');
      return;
    }
    context=Object.assign(context,await api('api/booking/prepare',{method:'POST',body:'{}'}));
    confirmationToken=context.confirmation_token || '';
    renderPassenger(context.profile_masked);
    $('bookingConsent').disabled=false;
    $('bookingBtn').disabled=true;
  }

  async function start() {
    try {
      if (tg) { tg.ready(); tg.expand(); }
      const token=new URLSearchParams(location.hash.slice(1)).get('token');
      if (token) {
        history.replaceState(null,'',location.pathname);
        if (!tg || !tg.initData) throw new Error('Защищённая форма открывается только кнопкой внутри Telegram.');
        const exchanged=await api('api/session/exchange',{method:'POST',body:JSON.stringify({token,init_data:tg.initData})});
        csrf=exchanged.csrf || '';
      }
      await loadContext();
    } catch (e) {
      $('loading').classList.add('hidden');
      $('profileCard').classList.remove('hidden');
      setStatus('profileStatus',e.message,'error');
    }
  }

  $('profileForm').addEventListener('submit', async e => {
    e.preventDefault(); $('saveBtn').disabled=true; setStatus('profileStatus','Шифрую и сохраняю…');
    try {
      await api('api/profile',{method:'POST',body:JSON.stringify(formData())});
      e.target.reset();
      setStatus('profileStatus','Профиль сохранён и зашифрован.','ok');
      await loadContext();
    } catch(err) { setStatus('profileStatus',err.message,'error'); }
    finally { $('saveBtn').disabled=false; }
  });

  $('deleteBtn').addEventListener('click', async () => {
    if (!confirm('Удалить профиль из рабочей базы? Восстановить его из интерфейса будет невозможно.')) return;
    try {
      const result=await api('api/profile',{method:'DELETE',body:'{}'});
      $('profileForm').reset(); $('existingCard').classList.add('hidden'); $('deleteBtn').classList.add('hidden');
      setStatus('profileStatus','Профиль удалён. '+result.backup_notice,'ok');
    } catch(err) { setStatus('profileStatus',err.message,'error'); }
  });

  $('bookingConsent').addEventListener('change', e => { $('bookingBtn').disabled=!e.target.checked || !confirmationToken; });
  $('bookingBtn').addEventListener('click', async () => {
    $('bookingBtn').disabled=true; setStatus('bookingStatus','Проверяю полные данные и создаю одну неоплаченную бронь на Туту…');
    try {
      const form=$('profileForm');
      for (const field of form.querySelectorAll('[required]')) {
        if (field.name !== 'storage_consent' && !field.checkValidity()) {
          field.reportValidity(); throw new Error('Повторно заполните все паспортные и контактные поля в форме выше.');
        }
      }
      const reviewedProfile={...formData(),storage_consent:true};
      const result=await api('api/booking/confirm',{method:'POST',body:JSON.stringify({
        confirmed:true,confirmation_token:confirmationToken,reviewed_profile:reviewedProfile
      })});
      form.reset();
      showPayment(result);
    } catch(err) { setStatus('bookingStatus',err.message,'error'); }
  });

  $('payLink').addEventListener('click', e => {
    if (!paymentUrl) { e.preventDefault(); return; }
    if (tg && tg.openLink) { e.preventDefault(); tg.openLink(paymentUrl); }
  });

  start();
})();
