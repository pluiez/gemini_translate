# Gemini Translate Diagrams

This directory contains various diagrams that illustrate the structure, flow, and architecture of the Gemini Translate application.

## Available Diagrams

### User Flow Sequence Diagram
- [HTML Version](user_flow_sequence_diagram.html)
- [Mermaid Version](user_flow_sequence_diagram.md)

### Translation Flow Sequence Diagram
- [HTML Version](translation_flow_sequence_diagram.html)
- [Mermaid Version](translation_flow_sequence_diagram.md)

This diagram illustrates the complete process flow from CSV file upload to translation and ZIP file download:
1. User uploads CSV file(s)
2. The system generates translation requests using generate_requests.py
3. Concurrent processing of requests via concurrent_api.py
4. Translation results are used to update the original data
5. Translated data is saved to CSV files and packaged in a ZIP file for download

## How to View

- **Mermaid Diagrams**: These can be viewed directly in GitHub or any markdown viewer that supports Mermaid syntax
- **HTML Diagrams**: Open these files in any web browser to view the interactive diagrams 