import tempfile,unittest
from pathlib import Path
from export_portfolio.portfolio_office import Application
from export_portfolio.intake.initiative import ContractError, DomainRecord

class ResourceBookTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.db=Path(t.name)/'db'
        self.a=Application(self.db);self.book=self.a.resource_book
        for p in ('a','b','c'):self.a.repository.save_record(DomainRecord(p,{'target_country':p}))
        self.book.capacity('partner',3)
        for p in ('a','b','c'):self.book.require(p,'partner',2)
    def test_competing_projects_and_idempotence(self):
        old=self.book.approve('one',['a'],{'partner':1})
        self.assertEqual(self.book.approve('one',['a'],{'partner':1}),old)
        with self.assertRaises(ContractError):self.book.approve('two',['b'],{'partner':1})
        with self.assertRaises(ContractError):self.book.approve('again',['a'],{'partner':1})
        with self.assertRaises(ContractError):self.book.require('a','partner',1)
        state=Application(self.db).resource_book.inspect()
        self.assertEqual(state['resources'][0]['available'],1);self.assertEqual(len(state['approvals']),1)
    def test_atomic_insufficiency_and_stale_commitment(self):
        with self.assertRaises(ContractError):self.book.approve('one',['a','b'],{'partner':1})
        self.assertEqual(self.book.inspect()['resources'][0]['available'],3)
        self.book.capacity('partner',4)
        with self.assertRaises(ContractError):self.book.approve('one',['a','b'],{'partner':1})
        self.book.approve('one',['a','b'],{'partner':2})
        self.assertEqual(self.book.inspect()['resources'][0]['available'],0)
        with self.assertRaises(ContractError):self.book.capacity('partner',3)
