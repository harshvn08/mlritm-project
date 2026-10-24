import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// Apply the saved theme before React paints to avoid a flash of the wrong palette.
const savedTheme = localStorage.getItem('atlas_theme');
document.documentElement.setAttribute('data-theme', savedTheme === 'dark' ? 'dark' : 'light');

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
