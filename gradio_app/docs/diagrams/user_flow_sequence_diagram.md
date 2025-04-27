# Gemini Translate User Flow Sequence Diagram

Below is a sequence diagram that illustrates the complete user journey through the Gemini Translate application, from opening the website to downloading the translated results.

```mermaid
sequenceDiagram
    participant User
    participant WebUI as Web UI
    participant Backend
    participant API as AI Model API
    
    User->>WebUI: 1. Open Website
    WebUI-->>User: Display Upload Interface
    
    User->>WebUI: 2. Upload CSV File
    WebUI->>Backend: Process File Upload
    Backend-->>WebUI: File Processed
    WebUI-->>User: Show CSV Preview
    
    User->>WebUI: 3. Select Target Language
    User->>WebUI: 4. Select AI Model (Gemini/OpenAI)
    User->>WebUI: 5. Configure API Parameters
    
    User->>WebUI: 6. Start Translation
    WebUI->>Backend: Initialize Translation Task
    
    Backend->>API: Send Translation Requests
    Note right of API: Process Multiple<br/>Items in Parallel
    
    Backend-->>WebUI: Progress Update (25%)
    WebUI-->>User: Display Progress (25%)
    
    Backend-->>WebUI: Progress Update (50%)
    WebUI-->>User: Display Progress (50%)
    
    Backend-->>WebUI: Progress Update (75%)
    WebUI-->>User: Display Progress (75%)
    
    API-->>Backend: Translation Results
    Backend-->>WebUI: Translation Complete
    WebUI-->>User: Show Translation Results
    
    User->>WebUI: 8. Download Results
    WebUI-->>User: CSV File Download
```

## Flow Description

1. **Website Access**: User opens the Gemini Translate web application
2. **Upload Data**: User uploads a CSV file containing product data for translation
3. **Configuration**: User selects target language, AI model, and sets API parameters
4. **Translation Initiation**: User starts the translation process
5. **Processing**: System processes translations in parallel, showing progress updates
6. **Completion**: Translation results are displayed to the user
7. **Download**: User downloads the translated CSV file

This sequence represents the complete user journey for the core translation workflow in Gemini Translate, supporting the affiliate marketing business model described in the business documentation. 