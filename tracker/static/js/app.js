function applyLanguage(lang) {
  document.querySelectorAll('[data-en][data-de]').forEach(function(el) {
    var text = el.getAttribute('data-' + lang);
    if (text !== null) {
      if ((el.tagName === 'INPUT' && el.type !== 'hidden') || el.tagName === 'TEXTAREA') {
        el.value = text;
      } else {
        el.innerText = text;
      }
    }
  });
  var btn = document.getElementById('langToggle');
  if (btn) {
    btn.innerText = lang === 'de' ? 'DE | EN' : 'EN | DE';
  }
}

function toggleLanguage() {
  var current = localStorage.getItem('lang') || 'de';
  var next = current === 'en' ? 'de' : 'en';
  localStorage.setItem('lang', next);
  applyLanguage(next);
}

document.addEventListener('DOMContentLoaded', function() {
  var lang = localStorage.getItem('lang') || 'de';
  applyLanguage(lang);
});

var NOTIFICATION_POLL_MS = 30000;

function fetchNotifications() {
  fetch('/api/notifications/').then(function(resp) {
    return resp.json();
  }).then(function(data) {
    if (data.ok) {
      updateNotificationBadge(data.count);
      return data.notifications;
    }
    return [];
  }).catch(function() {
    return [];
  });
}

function updateNotificationBadge(count) {
  var badges = document.querySelectorAll('.notification-count');
  badges.forEach(function(badge) {
    if (count > 0) {
      badge.textContent = count;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  });
}

function markNotificationRead(notifId, csrf) {
  var body = new URLSearchParams({ notification_id: notifId });
  fetch('/api/notifications/mark-read/', {
    method: 'POST',
    headers: { 'X-CSRFToken': csrf },
    body: body,
  });
}

function markAllNotificationsRead(csrf) {
  fetch('/api/notifications/mark-all-read/', {
    method: 'POST',
    headers: { 'X-CSRFToken': csrf },
  });
}

document.addEventListener('DOMContentLoaded', function() {
  fetchNotifications();
  setInterval(fetchNotifications, NOTIFICATION_POLL_MS);
});
