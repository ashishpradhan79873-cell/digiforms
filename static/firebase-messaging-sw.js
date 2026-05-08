/* eslint-disable no-undef */
importScripts("https://www.gstatic.com/firebasejs/9.22.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/9.22.2/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "AIzaSyArQ2f-2VsSxM9i6LryrUvD7ajuCEJ1pKc",
  authDomain: "myapplication-517480a3.firebaseapp.com",
  projectId: "myapplication-517480a3",
  storageBucket: "myapplication-517480a3.firebasestorage.app",
  messagingSenderId: "427607033950",
  appId: "1:427607033950:web:b78f6cd407d2840e3e0ccf",
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  const title = (payload && payload.notification && payload.notification.title) || "DigiForm";
  const body = (payload && payload.notification && payload.notification.body) || "";
  const icon = "/static/icons/icon-192.png";
  self.registration.showNotification(title, {
    body,
    icon,
    data: payload && payload.data ? payload.data : {},
  });
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("/"));
});

