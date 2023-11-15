import os
import uvicorn
from finance_app_database_service.api import app

def main():
    # print("YFinance Wrapper Starting Up!")
    # TEST_VARIABLE = os.getenv('TEST_VARIABLE')
    # print(TEST_VARIABLE)
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()