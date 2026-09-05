import React, { useEffect, useState } from 'react';
import axios from 'axios'; // If using Axios, else use fetch below

const MyComponent = () => {
    const [data, setData] = useState([]);

    useEffect(() => {
        // Using Axios
        axios.get('http://localhost:8000/api/my-model/')
            .then(response => {
                setData(response.data);
            })
            .catch(error => {
                console.error('Error fetching data:', error);
            });

        // Or using fetch (alternative to Axios)
        /*
        fetch('http://localhost:8000/api/my-model/')
            .then(response => response.json())
            .then(data => setData(data))
            .catch(error => console.error('Error fetching data:', error));
        */
    }, []);

    return (
        <div>
            <h1>Data from Django API:</h1>
            <ul>
                {data.map(item => (
                    <li key={item.id}>{item.name}</li>
                ))}
            </ul>
        </div>
    );
};

export default MyComponent;
