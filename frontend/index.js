import React from 'react';
import ReactDOM from 'react-dom';
import App from './src/App'; // Assuming you have an App component

// Rendering the App component into the root element
ReactDOM.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
  document.getElementById('root') // Make sure there's a div with id "root" in your HTML
);
